"""异步助记 worker：AnnotationJob 队列 → provider → Mnemonic（DESIGN.md §5）。

用法::

    # 离线跑一轮（默认 provider=fake，0 元）
    python -m app.annotate --db data/poi.db --ecdict data/ecdict.db --once
    # 常驻跟队列，每集预算 ¥4（DESIGN §5 预热预算制）
    python -m app.annotate --db data/poi.db --ecdict data/ecdict.db \
        --provider deepseek --loop --budget 4.0
    # 只看要花多少钱、prompt 长什么样，不发请求
    python -m app.annotate --db data/poi.db --provider deepseek --dry-run

流程（一批一批来）:
1. 取 queued 任务，**priority DESC, id ASC**（收藏 priority=10 插队，预热=0）；
2. 组装输入包：lemma + ECDICT 音标/释义 + 最近一条 Encounter 的原句/时间戳/集数；
   预热词没有 encounter，退回"当集任一含该词的 Segment 原句"；
   每个包带 id = AnnotationJob.id（对位靠它，见第 3 条）；
3. 按批调 provider.annotate()，输出**按 id 对回任务**（顺序无语义），逐条过 JSON
   schema；缺 id / id 不在本批 / 重复 id / 不合 schema 的元素丢弃，对应任务下一轮
   只重试没对上的那些；
4. 写 Mnemonic：context_gloss 单独一行 kind="gloss"，每个 hook 按 type 拆行存；
   version 递增；**edited_by_user=1 的那个 kind 永不被覆盖**；
5. job 置 done / failed（重试 --retries 次后仍失败才置 failed）。

预算制（工单 6-1 修）：estimate_cost 的累计与 --budget 检查发生在**每一次真实
provider 调用之前**（重试也算钱），不是每批只查一次——否则一批重试 3 次就能把
¥4 的预算烧成 ¥12。超限时低优先级任务原地停手、状态置回 queued 并计入
skipped_budget；高优先级（priority >= 10）不受预算限制，但花费照记。

估价 fail closed（工单 8b）：provider.estimate_cost 抛异常 / 返回 None / NaN /
inf / 负数时，**本轮立即停手**——任务保持（或放回）queued，一次 provider 调用
都不发，日志写明原因，进程退出码 3。不许再像以前那样"估价失败按 ¥0 处理"然后
照跑不误。高优先级（点击收藏）同样停：估价系统坏了就是坏了，不确定成本时
一分钱都不许花。

牌价（工单 16）：各 provider 的价目是一个带 currency/来源/as_of 的结构，估价结果
与 --dry-run 打印都会带上 as_of（"这是某天抄的牌价"要肉眼可见）。不改代码也能覆盖：
POI_DEEPSEEK_PRICE_IN/OUT、POI_ANTHROPIC_PRICE_IN/OUT（元 / 百万 token）。覆盖值
非法（负数 / NaN / 非数 / 空串）不会被忽略，而是走上面那条 fail closed 停手路径。

健壮性：单个任务/单个批次炸掉只影响它自己，worker 不崩；provider 抛什么异常都接。
本模块除 provider 外不发任何网络请求（provider=fake 时全程零网络）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from app.consts import (
    COLLECT_JOB_PRIORITY,
    DEFAULT_DB,
    DEFAULT_ECDICT,
    WEB_EPISODE,
)
from app.db import ENCOUNTER_SELECT, SOURCE_WEB, encounter_view, get_conn, init_db

# ECDICT 查询口径（word_lower + 优先本身小写那条）与 pos/释义取法只有一份实现，
# 住在 app/ecdict.py（工单 9 抽层）：worker 与 server 共用，且 worker **不再
# import web 层** —— 后台任务不该把 fastapi/starlette 整条链拖进来。
from app.ecdict import EcdictStore, fill_from_ecdict
from app.providers import (
    Provider,
    ProviderNotConfigured,
    get_provider,
    match_annotations,
)

DEFAULT_BUDGET = 4.0  # 元 / 每次 worker 运行（DESIGN §5：每集默认上限 ¥4）
DEFAULT_BATCH = 4
DEFAULT_RETRIES = 2  # 失败重试 2 次后置 failed
DEFAULT_POLL = 5.0

# priority >= 这个值算高优先级（= 用户点击收藏），不受预算限制
HIGH_PRIORITY = COLLECT_JOB_PRIORITY

GLOSS_KIND = "gloss"

# main() 的退出码：估价系统坏掉导致本轮停手（和普通失败区分开，方便脚本判断）
EXIT_ESTIMATE_BROKEN = 3


class EstimateUnavailable(RuntimeError):
    """estimate_cost 坏了：抛异常 / 返回 None / NaN / inf / 负数（工单 8b）。

    **绝不按 ¥0 处理**。不知道要花多少钱的时候一分钱都不许花——不管是预热的
    低优先级任务还是用户点击收藏的高优先级任务，一律停手、任务留在 queued。
    """


def price_info(provider: Any) -> tuple[str, str]:
    """(一行牌价说明, 短标注)。牌价是**某一天抄的一个数**，得让它露脸（工单 16-2）。

    provider 没有牌价概念（比如 fake）就返回两个空串，调用方跳过不打印；
    牌价配置坏了（比如 POI_DEEPSEEK_PRICE_IN=-1）也不在这儿抛——如实报"不可用"，
    真正的停手由 estimate_cost 走 fail closed 路径完成（工单 8b）。
    """
    fn = getattr(provider, "resolved_price", None)
    if not callable(fn):
        return "", ""
    try:
        price = fn()
        return str(price.label()), str(price.stamp())
    except Exception as exc:
        return f"牌价不可用：{type(exc).__name__}: {exc}", "牌价不可用"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class Job:
    """一条待处理任务 + 它的输入包。"""

    id: int
    lexeme_id: int
    priority: int
    lemma: str
    pack: dict = field(default_factory=dict)

    @property
    def high(self) -> bool:
        return self.priority >= HIGH_PRIORITY

    @property
    def pack_id(self) -> str:
        """输入/输出包的对位键。用 AnnotationJob.id：一批之内唯一、跨轮稳定。"""
        return str(self.id)


@dataclass
class RunStats:
    picked: int = 0
    done: int = 0
    failed: int = 0
    skipped_budget: int = 0
    skipped_estimate: int = 0  # 因估价不可用而没送出的任务（保持 queued，工单 8b）
    batches: int = 0
    calls: int = 0
    rows: int = 0
    est_cost: float = 0.0  # 已花估算（元）：每次真实 provider 调用前累加，含重试
    estimate_broken: bool = False  # 本轮是否因估价系统坏掉而停手

    @property
    def spent(self) -> float:
        """est_cost 的旧名字，留着不动老调用方。"""
        return self.est_cost

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["est_cost"] = round(self.est_cost, 4)
        d["spent"] = d["est_cost"]
        return d


class AnnotateWorker:
    """队列驱动的助记生成器。单进程单连接，不做并发（SQLite 单写者足够）。"""

    def __init__(
        self,
        db_path: str | Path,
        provider: Provider,
        ecdict_path: str | Path | None = None,
        budget: float = DEFAULT_BUDGET,
        batch_size: int = DEFAULT_BATCH,
        retries: int = DEFAULT_RETRIES,
        limit: int | None = None,
        content_id: int | None = None,
        log: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db_path = Path(db_path)
        self.provider = provider
        self.provider_name = getattr(provider, "name", provider.__class__.__name__)
        self.ecdict = EcdictStore(ecdict_path or DEFAULT_ECDICT)
        self.budget = float(budget)
        self.batch_size = max(1, int(batch_size))
        self.retries = max(0, int(retries))
        self.limit = limit
        self.content_id = content_id
        self.est_cost = 0.0  # 本次运行累计预估花费（元），每次真实调用前累加
        self._budget_logged = False
        # 估价系统坏掉的闸门（工单 8b）：一旦置位，本轮**所有**优先级都停手
        self.estimate_broken = False
        self._estimate_logged = False
        self._log = log if log is not None else self._default_log
        self._sleep = sleep
        self._conn: sqlite3.Connection | None = None

    # --- 基础设施 ----------------------------------------------------------

    @property
    def spent(self) -> float:
        """est_cost 的旧名字。"""
        return self.est_cost

    @staticmethod
    def _default_log(msg: str) -> None:
        print(f"[annotate] {msg}", flush=True)

    def log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:  # 日志失败绝不影响主流程
            pass

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            init_db(self.db_path).close()  # 幂等建表
            self._conn = get_conn(self.db_path)
        return self._conn

    def close(self) -> None:
        """关掉本 worker 开的所有 SQLite 连接（poi.db + ecdict.db）。

        Windows 上没关的连接会锁住文件，害得用户删不掉/重建不了词典（工单 6-4）。
        """
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self.ecdict.close_all()

    def __enter__(self) -> "AnnotateWorker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- 取任务 ------------------------------------------------------------

    def reset_stale_running(self) -> int:
        """上次被 Ctrl-C 掐死留下的 running 任务放回队列（单机单 worker 假设）。"""
        with self.conn as c:
            cur = c.execute(
                "UPDATE AnnotationJob SET status = 'queued' WHERE status = 'running'"
            )
        n = cur.rowcount or 0
        if n:
            self.log(f"重置 {n} 个僵死 running 任务 → queued")
        return n

    def queued_jobs(self, limit: int | None = None) -> list[Job]:
        """DESIGN §5 的取任务口径：queued，priority DESC，id ASC。"""
        sql = (
            "SELECT J.id, J.lexeme_id, J.priority, L.lemma FROM AnnotationJob J "
            "JOIN Lexeme L ON L.id = J.lexeme_id "
            "WHERE J.status = 'queued' ORDER BY J.priority DESC, J.id ASC"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [
            Job(
                id=int(r["id"]),
                lexeme_id=int(r["lexeme_id"]),
                priority=int(r["priority"]),
                lemma=r["lemma"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    # --- 组装输入包（DESIGN §5 输入形状） ----------------------------------

    def _latest_encounter(self, lexeme_id: int) -> dict[str, Any] | None:
        """最近一条**有原句**的 encounter（字幕段或网页划词，工单 11）。

        为什么不是"最近一条"：网页收藏偶尔截不到整句（sentence 为空），
        那条不该把上一次有原句的语境挤掉——模型没句子就只能瞎编。
        网页来源的 episode 固定写 "web"，时间戳无意义（None）。
        """
        rows = self.conn.execute(
            ENCOUNTER_SELECT
            + "JOIN VocabEntry V ON V.id = E.vocab_entry_id "
            "WHERE V.lexeme_id = ? ORDER BY E.id DESC LIMIT 20",
            (lexeme_id,),
        ).fetchall()
        for r in rows:
            view = encounter_view(r)
            if not view["sentence"]:
                continue
            web = view["source_kind"] == SOURCE_WEB
            return {
                "surface": view["surface"],
                "sentence": view["sentence"],
                "t_start": None if web else view["t_start"],
                "episode": WEB_EPISODE if web else view["season_ep"],
            }
        return None

    def _any_segment_with(self, lemma: str) -> sqlite3.Row | None:
        """预热词没有 encounter：拿当集任一含该词的字幕段当原句。

        匹配的是 ingest 写进 Segment.tokens_json 的 `"lemma": "xxx"` 片段
        （json.dumps 默认分隔符，形状稳定），不做整句正则。
        """
        sql = (
            "SELECT S.text_en, S.t_start, C.season_ep, C.id AS content_id "
            "FROM Segment S JOIN Content C ON C.id = S.content_id "
            "WHERE S.tokens_json LIKE ? ESCAPE '\\'"
        )
        params: list[Any] = ['%"lemma": "' + _like_escape(lemma) + '"%']
        if self.content_id is not None:
            sql += " AND S.content_id = ?"
            params.append(self.content_id)
        sql += " ORDER BY S.content_id, S.idx LIMIT 1"
        return self.conn.execute(sql, params).fetchone()

    def build_pack(self, job: Job) -> dict:
        """一词一包。缺字段就是 None——provider 侧要能吃下 None。"""
        lex = self.conn.execute(
            "SELECT id, lemma, pos, ipa, dict_gloss FROM Lexeme WHERE id = ?",
            (job.lexeme_id,),
        ).fetchone()
        if lex is None:
            raise LookupError(f"lexeme {job.lexeme_id} 不存在")
        lemma = lex["lemma"]
        # ECDICT 回填（顺手把 Lexeme 缓存补齐，生词本立刻能显示释义）
        fields, _in_dict = fill_from_ecdict(self.conn, self.ecdict, lex, lemma, lemma)

        enc = self._latest_encounter(job.lexeme_id)
        if enc is not None:
            surface, sentence = enc["surface"], enc["sentence"]
            t, episode = enc["t_start"], enc["episode"]
        else:
            seg = self._any_segment_with(lemma)
            surface = lemma
            sentence = seg["text_en"] if seg is not None else None
            t = seg["t_start"] if seg is not None else None
            episode = seg["season_ep"] if seg is not None else None
            if seg is None:
                self.log(f"lemma={lemma!r} 既无 encounter 也无字幕原句，裸词送模型")

        return {
            # 对位键：模型必须原样回传（顺序不作数，见 providers.match_annotations）
            "id": job.pack_id,
            "lemma": lemma,
            "surface": surface or lemma,
            "pos": fields["pos"],
            "ipa": fields["ipa"],
            "dict_gloss": fields["dict_gloss"],
            "sentence": sentence,
            "speaker": None,  # OCR 拿不到可靠说话人（DESIGN §5）
            "episode": episode,
            "t": round(float(t), 3) if t is not None else None,
        }

    # --- 写 Mnemonic -------------------------------------------------------

    def _protected_kinds(self, lexeme_id: int) -> set[str]:
        """最新版本被用户编辑过的 kind：这些行永不被新一代覆盖（DESIGN §5）。"""
        rows = self.conn.execute(
            "SELECT kind, version, edited_by_user FROM Mnemonic WHERE lexeme_id = ? "
            "ORDER BY kind, version DESC",
            (lexeme_id,),
        ).fetchall()
        protected: set[str] = set()
        seen: set[str] = set()
        for r in rows:
            if r["kind"] in seen:
                continue
            seen.add(r["kind"])
            if int(r["edited_by_user"]):
                protected.add(r["kind"])
        return protected

    def _next_version(self, lexeme_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM Mnemonic WHERE lexeme_id = ?",
            (lexeme_id,),
        ).fetchone()
        return int(row["v"]) + 1

    @staticmethod
    def to_rows(out: dict) -> list[tuple[str, dict]]:
        """输出包 → [(kind, payload)]。

        context_gloss 单独一行 kind="gloss"；每个 hook 按 type 拆行。
        同一 type 出现多条时后面的加 "#2" 后缀——(lexeme, kind, version) 是唯一键，
        不加后缀会撞车丢内容。
        """
        rows: list[tuple[str, dict]] = [
            (GLOSS_KIND, {"text": out["context_gloss"]})
        ]
        seen: dict[str, int] = {}
        for hook in out.get("hooks", []):
            t = hook.get("type") or "hook"
            seen[t] = seen.get(t, 0) + 1
            kind = t if seen[t] == 1 else f"{t}#{seen[t]}"
            rows.append((kind, dict(hook)))
        return rows

    def write_result(self, job: Job, out: dict) -> int:
        """落库，返回真正写进去的行数。被保护的 kind 会被跳过。"""
        protected = self._protected_kinds(job.lexeme_id)
        version = self._next_version(job.lexeme_id)
        written = 0
        with self.conn as c:
            for kind, payload in self.to_rows(out):
                if kind in protected:
                    self.log(
                        f"lemma={job.lemma!r} kind={kind} 已被用户编辑，跳过覆盖"
                    )
                    continue
                c.execute(
                    "INSERT INTO Mnemonic "
                    "(lexeme_id, kind, payload_json, provider, version, edited_by_user) "
                    "VALUES (?,?,?,?,?,0)",
                    (
                        job.lexeme_id,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        self.provider_name,
                        version,
                    ),
                )
                written += 1
        return written

    def _set_status(self, job_ids: Sequence[int], status: str) -> None:
        if not job_ids:
            return
        marks = ",".join("?" * len(job_ids))
        done_at = _now() if status in ("done", "failed") else None
        try:
            with self.conn as c:
                c.execute(
                    f"UPDATE AnnotationJob SET status = ?, done_at = ? "
                    f"WHERE id IN ({marks})",
                    [status, done_at, *job_ids],
                )
        except sqlite3.Error as exc:  # 落库失败也不许炸 worker
            self.log(f"更新任务状态失败({status}): {exc}")

    # --- 跑一批 ------------------------------------------------------------

    def _afford(self, pending: list[Job], stats: RunStats) -> bool:
        """这次**真实调用**掏不掏得起？掏得起就当场记账（重试也计费）。

        高优先级（用户点击收藏）不受预算限制，但花费照记——否则 est_cost 会骗人。

        估价本身坏掉（工单 8b）时返回 False 并置 self.estimate_broken：
        这一票否决**不分优先级**，高优先级也停——估价系统坏了就是坏了。
        """
        try:
            est = self._estimate(pending)
        except EstimateUnavailable as exc:
            self.estimate_broken = True
            stats.estimate_broken = True
            if not self._estimate_logged:
                self.log(
                    f"估价不可用，**停止本轮处理**（绝不按 ¥0 继续）：{exc}；"
                    f"任务保持 queued，修好 provider.estimate_cost 后重跑"
                )
                self._estimate_logged = True
            return False
        if not any(j.high for j in pending) and self.est_cost + est > self.budget:
            if not self._budget_logged:
                self.log(
                    f"预算已用尽（累计 ¥{self.est_cost:.4f} + 本次调用 ¥{est:.4f} "
                    f"> 上限 ¥{self.budget:.2f}）：停手，剩下的低优先级任务保持 "
                    f"queued（调高 --budget 后可继续）"
                )
                self._budget_logged = True
            return False
        self.est_cost += est
        stats.est_cost = self.est_cost
        return True

    def process_batch(self, batch: list[Job], stats: RunStats) -> bool:
        """一批的完整生命周期：running → 调用（含重试）→ 落库 → done/failed。

        返回 True 表示**没送出去就停手了**（本批没跑完的任务已置回 queued）：
        要么预算用尽（调用方停止再送低优先级任务），要么估价系统坏掉
        （self.estimate_broken 置位，调用方连高优先级都不许再送，工单 8b）。
        """
        if not batch:
            return False
        if self.estimate_broken:  # 闸门已落：一条都不许再送
            self._set_status([j.id for j in batch], "queued")
            stats.skipped_estimate += len(batch)
            stats.estimate_broken = True
            return True
        stats.batches += 1
        self._set_status([j.id for j in batch], "running")

        pending = list(batch)
        results: dict[int, dict] = {}
        last_err = ""
        budget_stop = False
        for attempt in range(self.retries + 1):
            if not pending:
                break
            # 预算检查/记账必须在**每次**真实调用之前，重试也不例外（工单 6-1）
            if not self._afford(pending, stats):
                budget_stop = True
                last_err = (
                    "估价不可用，未送出" if self.estimate_broken else "预算用尽，未送出"
                )
                break
            if attempt:
                self.log(
                    f"重试第 {attempt}/{self.retries} 次："
                    f"{[j.lemma for j in pending]}（上次: {last_err[:120]}）"
                )
            try:
                stats.calls += 1
                outs = self.provider.annotate([dict(j.pack) for j in pending])
            except ProviderNotConfigured as exc:
                # 缺 key 重试一万次也没用，直接判死并把话说清楚
                last_err = str(exc)
                self.log(f"provider 未配置：{exc}")
                break
            except Exception as exc:  # provider 想抛什么抛什么，worker 不倒
                last_err = f"{type(exc).__name__}: {exc}"
                self.log(f"provider 调用失败：{last_err}")
                continue
            # 按 id 对位，不看顺序（工单 6-2：模型重排过就会张冠李戴）
            matched, problems = match_annotations([j.pack for j in pending], outs)
            if problems:
                last_err = "；".join(problems[:3])
                self.log(f"丢弃 {len(problems)} 个对不上的输出元素：{last_err[:200]}")
            still: list[Job] = []
            for job in pending:
                out = matched.get(job.pack_id)
                if out is None:
                    still.append(job)
                else:
                    results[job.id] = out
            if not matched and not problems:
                last_err = "provider 返回了空数组"
                self.log(f"provider 没吐出任何元素：{[j.lemma for j in pending]}")
            pending = still

        skipped: list[Job] = []
        if budget_stop and pending:
            skipped = list(pending)
            self._set_status([j.id for j in skipped], "queued")
            if self.estimate_broken:
                stats.skipped_estimate += len(skipped)
                self.log(f"估价不可用：{[j.lemma for j in skipped]} 放回队列")
            else:
                stats.skipped_budget += len(skipped)
                self.log(f"预算截断：{[j.lemma for j in skipped]} 放回队列")

        ok_ids: list[int] = []
        for job in batch:
            out = results.get(job.id)
            if out is None:
                continue
            try:
                n = self.write_result(job, out)
            except Exception as exc:  # 单条落库失败只算这条失败
                self.log(f"lemma={job.lemma!r} 落库失败：{type(exc).__name__}: {exc}")
                continue
            stats.rows += n
            ok_ids.append(job.id)
            self.log(f"lemma={job.lemma!r} -> done（{n} 行，version 见库）")

        skipped_ids = {j.id for j in skipped}
        failed = [j for j in batch if j.id not in ok_ids and j.id not in skipped_ids]
        self._set_status(ok_ids, "done")
        self._set_status([j.id for j in failed], "failed")
        stats.done += len(ok_ids)
        stats.failed += len(failed)
        for j in failed:
            self.log(f"lemma={j.lemma!r} -> failed（{last_err[:160] or '未知原因'}）")
        return budget_stop

    # --- 跑一轮 ------------------------------------------------------------

    def _prepare(self, jobs: list[Job], stats: RunStats) -> list[Job]:
        """给任务装上输入包；装不上的直接判 failed（不占批次）。"""
        ready: list[Job] = []
        for job in jobs:
            try:
                job.pack = self.build_pack(job)
            except Exception as exc:
                self.log(f"job {job.id} 组包失败：{type(exc).__name__}: {exc}")
                self._set_status([job.id], "failed")
                stats.failed += 1
                continue
            ready.append(job)
        return ready

    def _estimate(self, batch: list[Job]) -> float:
        """估价，**fail closed**（工单 8b）。

        estimate_cost 抛异常 / 返回 None / NaN / inf / 负数 → 抛 EstimateUnavailable。
        以前这里 return 0.0，等于"估价系统坏了就当免费"，能让一整轮任务按 ¥0 跑
        进真 API。现在不确定成本时一分钱都不花。
        """
        try:
            raw = self.provider.estimate_cost([dict(j.pack) for j in batch])
        except Exception as exc:
            raise EstimateUnavailable(
                f"estimate_cost 抛异常 {type(exc).__name__}: {exc}"
            ) from exc
        if raw is None or isinstance(raw, bool):
            raise EstimateUnavailable(f"estimate_cost 返回 {raw!r}（不是数）")
        try:
            val = float(raw)
        except (TypeError, ValueError) as exc:
            raise EstimateUnavailable(
                f"estimate_cost 返回 {raw!r}，转不成数字: {exc}"
            ) from exc
        if math.isnan(val) or math.isinf(val):
            raise EstimateUnavailable(f"estimate_cost 返回 {val}（NaN/inf）")
        if val < 0:
            raise EstimateUnavailable(f"estimate_cost 返回负数 {val}")
        return val

    def run_once(self) -> RunStats:
        """处理当前队列里能处理的全部任务，返回统计。不抛异常。"""
        stats = RunStats()
        try:
            jobs = self.queued_jobs(self.limit)
        except sqlite3.Error as exc:
            self.log(f"读队列失败：{exc}")
            return stats
        if not jobs:
            return stats
        stats.picked = len(jobs)

        high = self._prepare([j for j in jobs if j.high], stats)
        low = self._prepare([j for j in jobs if not j.high], stats)

        # 高优先级（用户点击收藏的词）不受预算限制，永远先跑（花费仍然记账）。
        # 唯一能拦住它们的是估价系统坏掉（工单 8b）——那时连收藏也不许花钱。
        for i in range(0, len(high), self.batch_size):
            batch = high[i : i + self.batch_size]
            self.process_batch(batch, stats)
            if self.estimate_broken:
                rest = high[i + len(batch) :] + low
                if rest:
                    stats.skipped_estimate += len(rest)
                    self._set_status([j.id for j in rest], "queued")
                self.log(
                    f"估价不可用：本轮停手，还有 {len(rest)} 个任务（含高优先级）"
                    f"保持 queued"
                )
                stats.est_cost = self.est_cost
                return stats

        # 低优先级（预热）按预算截断。截断判定在 process_batch 里逐次调用做，
        # 这里只负责收摊：剩下的批次一个都不送，全部保持 queued。
        for i in range(0, len(low), self.batch_size):
            batch = low[i : i + self.batch_size]
            if self.process_batch(batch, stats):
                rest = low[i + len(batch) :]
                if rest:
                    if self.estimate_broken:
                        stats.skipped_estimate += len(rest)
                        self.log(
                            f"估价不可用：还有 {len(rest)} 个任务保持 queued"
                        )
                    else:
                        stats.skipped_budget += len(rest)
                        self.log(
                            f"预算截断：还有 {len(rest)} 个低优先级任务保持 queued"
                        )
                break

        stats.est_cost = self.est_cost
        return stats

    def run_loop(self, poll: float = DEFAULT_POLL, max_rounds: int | None = None) -> RunStats:
        """常驻跟队列。Ctrl-C 干净退出；空转就睡 poll 秒。"""
        total = RunStats()
        rounds = 0
        try:
            while max_rounds is None or rounds < max_rounds:
                rounds += 1
                s = self.run_once()
                total.picked += s.picked
                total.done += s.done
                total.failed += s.failed
                total.skipped_budget += s.skipped_budget
                total.skipped_estimate += s.skipped_estimate
                total.batches += s.batches
                total.calls += s.calls
                total.rows += s.rows
                total.est_cost = self.est_cost
                if s.estimate_broken:
                    # 估价坏了不是"等一等就好"的事，接着轮询只会刷屏。退出让人来修。
                    total.estimate_broken = True
                    self.log("估价不可用：退出 --loop（任务全部保持 queued）")
                    break
                if s.done == 0 and s.failed == 0:
                    self._sleep(poll)
        except KeyboardInterrupt:
            self.log("收到 Ctrl-C，退出")
        return total

    # --- dry-run -----------------------------------------------------------

    def dry_run(self) -> dict:
        """只组包 + 估价，不调 provider、不写库、不改任务状态。"""
        jobs = self._prepare_dry(self.queued_jobs(self.limit))
        error = ""
        # 牌价说明（含 as_of）跟着估价一起报——光看一个 ¥ 数字不知道它按哪天的价算的
        price, price_stamp = price_info(self.provider)
        try:
            est = self._estimate(jobs) if jobs else 0.0
        except EstimateUnavailable as exc:  # 估价坏了就如实说，别报一个假的 0
            error = str(exc)
            self.log(f"dry-run 估价不可用：{exc}")
            return {
                "jobs": len(jobs),
                "estimate_cny": None,
                "estimate_error": error,
                "budget": self.budget,
                "over_budget": True,  # 不知道多贵 = 当作贵到超预算
                "price": price,
                "price_as_of": price_stamp,
                "packs": [j.pack for j in jobs],
            }
        return {
            "jobs": len(jobs),
            "estimate_cny": round(est, 4),
            "estimate_error": "",
            "budget": self.budget,
            "over_budget": est > self.budget,
            "price": price,
            "price_as_of": price_stamp,
            "packs": [j.pack for j in jobs],
        }

    def _prepare_dry(self, jobs: list[Job]) -> list[Job]:
        out = []
        for job in jobs:
            try:
                job.pack = self.build_pack(job)
            except Exception as exc:
                self.log(f"job {job.id} 组包失败：{exc}")
                continue
            out.append(job)
        return out


# --- CLI -------------------------------------------------------------------


def build_provider(name: str, model: str | None = None) -> Provider:
    """按名造 provider。真 provider 的内部重试关掉——重试策略统一归 worker，
    否则 worker 重试 × provider 重试 = 9 次调用，钱包受不了。"""
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    if name != "fake":
        kwargs["retries"] = 0
    try:
        return get_provider(name, **kwargs)
    except TypeError:  # 自定义 provider 不认这些参数
        return get_provider(name)


def payload_sample(provider: Provider, packs: Sequence[dict], batch_size: int) -> dict | None:
    """provider **真会发出去**的请求体，离线组装：不发包、不读 key、不含鉴权头。

    上线前用它眼验两件最花钱的事：模型名对不对（--model 有没有真的透传到请求体）、
    thinking 有没有开（工单 8a）。messages 里全是 prompt 正文，这儿折成一行摘要，
    想看正文用 provider.dump_prompt(batch)。provider 没有 payload()（比如 fake）
    就返回 None，调用方跳过这行。
    """
    fn = getattr(provider, "payload", None)
    if not callable(fn) or not packs:
        return None
    try:
        body = fn([dict(p) for p in packs[: max(1, batch_size)]])
    except Exception:  # 组装失败不许拖垮 dry-run
        return None
    if not isinstance(body, dict):
        return None
    out = dict(body)
    msgs = out.get("messages")
    if isinstance(msgs, list):
        chars = sum(len(str(m.get("content", ""))) for m in msgs if isinstance(m, dict))
        out["messages"] = f"<{len(msgs)} 条 / 共 {chars} 字符，正文见 dump_prompt()>"
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.annotate",
        description="助记生成 worker（AnnotationJob 队列驱动，DESIGN §5）",
    )
    ap.add_argument("--db", default=os.environ.get("POI_DB", DEFAULT_DB))
    ap.add_argument("--ecdict", default=os.environ.get("POI_ECDICT", DEFAULT_ECDICT))
    ap.add_argument("--provider", default="fake", help="fake / anthropic / deepseek")
    ap.add_argument("--model", default=None, help="覆盖 provider 默认模型")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="跑一轮就退出（默认）")
    mode.add_argument("--loop", action="store_true", help="常驻跟队列")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                    help=f"低优先级任务的累计预算上限，人民币元（默认 {DEFAULT_BUDGET}）")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"单批失败重试次数（默认 {DEFAULT_RETRIES}，超了置 failed）")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多取多少任务")
    ap.add_argument("--content-id", type=int, default=None,
                    help="预热词找原句时限定在这一集里找")
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL, help="--loop 空转睡多久")
    ap.add_argument("--dry-run", action="store_true",
                    help="只组包 + 估价，不调 provider、不写库")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        provider = build_provider(args.provider, args.model)
    except Exception as exc:
        print(f"[annotate] provider 初始化失败：{exc}")
        return 2

    log = (lambda _m: None) if args.quiet else None
    worker = AnnotateWorker(
        db_path=args.db,
        provider=provider,
        ecdict_path=args.ecdict,
        budget=args.budget,
        batch_size=args.batch_size,
        retries=args.retries,
        limit=args.limit,
        content_id=args.content_id,
        log=log,
    )
    _, price_stamp = price_info(provider)
    with worker:
        if not args.quiet:
            print(
                f"[annotate] db={args.db} provider={worker.provider_name} "
                f"budget=¥{args.budget:.2f} batch={args.batch_size}"
                + (f" {price_stamp}" if price_stamp else "")
            )
        if args.dry_run:
            info = worker.dry_run()
            if info.get("estimate_error"):
                print(
                    f"[annotate] dry-run: {info['jobs']} 个任务，"
                    f"**估价不可用**：{info['estimate_error']}"
                )
            else:
                print(
                    f"[annotate] dry-run: {info['jobs']} 个任务，"
                    f"预估 ¥{info['estimate_cny']}（预算 ¥{info['budget']:.2f}）"
                )
            if info.get("price"):
                print(f"  牌价(估算依据): {info['price']}")
            for pack in info["packs"][:10]:
                print("  " + json.dumps(pack, ensure_ascii=False))
            sample = payload_sample(provider, info["packs"], args.batch_size)
            if sample is not None:
                print("  请求体样例(不发送): " + json.dumps(sample, ensure_ascii=False))
            return EXIT_ESTIMATE_BROKEN if info.get("estimate_error") else 0

        worker.reset_stale_running()
        stats = worker.run_loop(poll=args.poll) if args.loop else worker.run_once()

    d = stats.as_dict()
    print(
        f"[annotate] done={d['done']} failed={d['failed']} "
        f"skipped_budget={d['skipped_budget']} "
        f"skipped_estimate={d['skipped_estimate']} rows={d['rows']} "
        f"calls={d['calls']} est_cost=¥{d['est_cost']}（预算 ¥{args.budget:.2f}，"
        f"含重试的每次调用都计费"
        + (f"，{price_stamp}" if price_stamp else "")
        + "）"
    )
    if d["estimate_broken"]:
        print(
            "[annotate] 估价不可用：本轮已停手，任务全部保持 queued，"
            "一分钱没花。修好 provider.estimate_cost 再重跑。"
        )
        return EXIT_ESTIMATE_BROKEN
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
