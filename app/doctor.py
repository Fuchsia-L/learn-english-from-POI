"""`python -m app.doctor`：只读体检（工单 13）。

一条命令回答「这个库现在能不能用、哪儿烂了」：视频在不在、时间轴是否自洽、
词框覆盖到哪、tokens 与词框对不对得上、词典能不能查、外键有没有悬空。

**只读是硬约束**：
- 主库一律以 `file:...?mode=ro` URI 打开（sqlite 层面拒绝任何写），
  绝不 init_db / 建表 / 迁移——体检不该顺手改坏一个本来只是想看看的库；
- ecdict 走 app.ecdict.EcdictStore（本来就是 mode=ro）；
- 除了 ffprobe 读视频头，不碰任何文件。
（ro 连接会在库旁边生成 -wal/-shm 边车文件，那是 sqlite 读 WAL 库的必需品，
  .db 主文件字节不变——tests/test_doctor.py 有哈希断言守着。）

**老库（v1）**：先看 `PRAGMA user_version` 与 Encounter 的列，再跑任何查询。
Encounter 缺 source_kind/context_json 的 v1 库上，体检报 ✗ 并写清楚怎么迁
（只读原则：doctor 自己一个字都不改），跳过依赖这两列的检查，退出码非零 ——
而不是像以前那样抛一个 `no such column: source_kind` 的 traceback（工单 17-3）。

用法:
    python -m app.doctor                       # 体检 data/poi.db 全部剧集
    python -m app.doctor --db /tmp/poi.db --content-id 3
    python -m app.doctor --json                # 机器可读完整报告
    python -m app.doctor --strict              # ⚠ 也算失败（CI 用）
    python -m app.doctor --no-ffprobe          # 跳过媒体探测（快）

退出码: 0 = 通过（可能有 ⚠）；1 = 有 ✗（--strict 下 ⚠ 同样非零）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.consts import DEFAULT_DB, DEFAULT_ECDICT
from app.db import SCHEMA_VERSION, TABLES
from app.ecdict import EcdictStore
from app.ingest import normalize_surface

__all__ = ["run_doctor", "Report", "Finding", "main"]

# --- 判定阈值（都在这儿，改口径只改这一块） --------------------------------

# 词框坐标的参考系：extract_hardsub.py 默认 --crop 面向 1920x1080 源，
# 坐标是**视频原始帧像素**，所以越界判定按这个参考系来。
# 真·4K 片源会整片报越界 —— 那就是提醒你该改 crop / 改这个常量，不是误报。
REF_W, REF_H = 1920, 1080

# 字幕尾巴超过视频时长多少秒才算越界（编码时长与容器时长本来就有零点几秒出入）
DURATION_SLACK = 0.5
# 相邻段重叠容忍（浮点误差级别）
OVERLAP_EPS = 1e-3
# 空坐标（OCR 丢框）占比超过这个才升级为 ⚠；丢框不丢词，少量属正常磨损
NULL_BOX_WARN_RATIO = 0.05
# 有框段里对不齐的比例超过这个 → ✗（多半是 boxes 与 srt 不是同一批产物）
MISALIGN_FAIL_RATIO = 0.5
# 词框覆盖率低于这个 → ⚠（前端退回自渲染热区，能用但体验降级）
COVERAGE_WARN_RATIO = 1.0
# ffprobe 单个文件超时（秒）
FFPROBE_TIMEOUT = 20
# 报告里每类问题最多举几个例子
MAX_EXAMPLES = 5
# ECDICT 抽样查询的词条数
ECDICT_SAMPLE = 5

# 与 app.ingest._TOKEN_RE 逐字一致：以字母开头结尾、内部允许撇号/连字符。
# 这里不 import 私名（那是 ingest 的内部实现），但 tests/test_doctor.py 有一条
# 断言把两边的 pattern 钉在一起，谁改了谁红。
WORD_RE = re.compile(r"[^\W\d_]+(?:['’ʼ\-][^\W\d_]+)*", re.UNICODE)

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "⚠", FAIL: "✗"}
_RANK = {OK: 0, WARN: 1, FAIL: 2}


class DoctorError(Exception):
    """体检没法开始（库打不开之类）。"""


# --- 报告容器 --------------------------------------------------------------


@dataclass
class Finding:
    level: str  # ok / warn / fail
    section: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.level,
            "section": self.section,
            "message": self.message,
        }
        if self.data:
            out["data"] = self.data
        return out


class Report:
    """findings（分节的 ✓/⚠/✗ 条目）+ data（结构化统计）。"""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.data: dict[str, Any] = {}
        self._sections: list[str] = []

    def add(self, level: str, section: str, message: str, **data: Any) -> Finding:
        if section not in self._sections:
            self._sections.append(section)
        f = Finding(level, section, message, data)
        self.findings.append(f)
        return f

    def ok(self, section: str, message: str, **data: Any) -> Finding:
        return self.add(OK, section, message, **data)

    def warn(self, section: str, message: str, **data: Any) -> Finding:
        return self.add(WARN, section, message, **data)

    def fail(self, section: str, message: str, **data: Any) -> Finding:
        return self.add(FAIL, section, message, **data)

    def counts(self) -> dict[str, int]:
        c = {OK: 0, WARN: 0, FAIL: 0}
        for f in self.findings:
            c[f.level] += 1
        return c

    @property
    def verdict(self) -> str:
        return max((f.level for f in self.findings), key=lambda lv: _RANK[lv], default=OK)

    def exit_code(self, strict: bool = False) -> int:
        v = self.verdict
        if v == FAIL:
            return 1
        if strict and v == WARN:
            return 1
        return 0

    def sections(self) -> list[tuple[str, list[Finding]]]:
        return [
            (s, [f for f in self.findings if f.section == s]) for s in self._sections
        ]

    def to_json(self, strict: bool = False) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "exit_code": self.exit_code(strict),
            "counts": self.counts(),
            "findings": [f.to_json() for f in self.findings],
            "data": self.data,
        }


# --- 只读连接 --------------------------------------------------------------


def open_readonly(path: Path) -> sqlite3.Connection:
    """mode=ro 打开。库不存在 / 不是 sqlite 文件 → DoctorError。"""
    if not path.exists():
        raise DoctorError(f"数据库不存在: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as e:
        raise DoctorError(f"数据库打不开（损坏？）: {path}: {e}") from e
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row[0]) if row is not None else 0


def _human_size(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < step or unit == "GB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{val:.1f} GB"


# --- 1. schema -------------------------------------------------------------


# v2 才有的列（工单 11 的 Encounter 泛化）。doctor 只读、绝不迁移，所以遇到
# 老库只能报告，不能顺手改——但也不能像以前那样一头撞进 "no such column:
# source_kind" 的 traceback 里（工单 17-3）。
V2_ENCOUNTER_COLUMNS = ("source_kind", "context_json")

# 老库该怎么迁：新版服务打开一次库就会自动迁（Database._ensure_schema → init_db），
# 想手动来一下就跑这条命令。doctor 自己什么都不做。
MIGRATE_HINT = (
    "用新版服务打开一次这个库即可自动迁移"
    "（uvicorn app.server:app / python -m app.server），"
    "或手动跑 python -c \"from app.db import init_db; init_db('<db>').close()\""
)


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """PRAGMA table_info → 列名。表不存在就是空列表（PRAGMA 不会抛）。"""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def encounter_v2_gap(conn: sqlite3.Connection) -> list[str]:
    """Encounter 少了哪些 v2 列（空列表 = 已经是 v2 形状）。"""
    have = set(table_columns(conn, "Encounter"))
    return [c for c in V2_ENCOUNTER_COLUMNS if c not in have]


def check_schema(conn: sqlite3.Connection, report: Report) -> bool:
    """表齐不齐 + user_version + 列形状。没法往下体检就返回 False。

    顺序是有讲究的：**先看 PRAGMA user_version 和 Encounter 的列，再跑任何查询**。
    v1 老库（Encounter 没有 source_kind/context_json）上，后面那些 SQL 会直接
    抛 "no such column"——那是一坨 traceback，不是体检报告。
    """
    sec = "schema"
    have = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = [t for t in TABLES if t not in have]
    ver = _scalar(conn, "PRAGMA user_version")
    gap = encounter_v2_gap(conn) if "Encounter" in have else []
    report.data["schema"] = {
        "tables_present": sorted(have),
        "tables_missing": missing,
        "user_version": ver,
        "expected_version": SCHEMA_VERSION,
        "encounter_columns": table_columns(conn, "Encounter") if "Encounter" in have else [],
        "encounter_missing": gap,
        "needs_migration": bool(gap),
    }
    if missing:
        report.fail(sec, f"缺 {len(missing)} 张表: {', '.join(missing)}", missing=missing)
        return False

    if gap:
        # 老库：报告清楚 + 非零退出（✗），但**不迁移**，也不再跑依赖这些列的检查。
        report.fail(
            sec,
            f"老库（schema v{ver}，当前代码是 v{SCHEMA_VERSION}）："
            f"Encounter 缺 {', '.join(gap)} 列。doctor 只读，不会替你迁移——"
            f"{MIGRATE_HINT}；迁完再体检。",
            user_version=ver,
            expected=SCHEMA_VERSION,
            encounter_missing=gap,
        )
        return True  # 媒体/时间轴/词框这些与 Encounter 无关的检查照常往下走

    if ver != SCHEMA_VERSION:
        report.warn(
            sec,
            f"user_version={ver}，当前代码期望 {SCHEMA_VERSION}"
            f"（表结构已经是新的，只是版本号没盖上；{MIGRATE_HINT}）",
            user_version=ver,
            expected=SCHEMA_VERSION,
        )
    else:
        report.ok(sec, f"{len(TABLES)} 张表齐全，schema v{ver}")
    return True


# --- 2. 视频文件 + ffprobe -------------------------------------------------


def probe_video(path: Path) -> dict[str, Any]:
    """ffprobe 读容器信息。返回 {ok, error?, duration, video[], audio[]}。"""
    exe = shutil.which("ffprobe")
    if exe is None:
        return {"ok": False, "skipped": True, "error": "ffprobe 不在 PATH"}
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"ffprobe 执行失败: {e}"}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": err[-1] if err else f"ffprobe 退出码 {proc.returncode}"}
    try:
        info = json.loads(proc.stdout or "{}")
    except ValueError as e:
        return {"ok": False, "error": f"ffprobe 输出不是 JSON: {e}"}

    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    duration = None
    for cand in (fmt.get("duration"), *(s.get("duration") for s in streams)):
        try:
            duration = float(cand)  # type: ignore[arg-type]
            break
        except (TypeError, ValueError):
            continue
    pick = lambda kind: [  # noqa: E731
        {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height")}
        for s in streams
        if s.get("codec_type") == kind
    ]
    return {
        "ok": True,
        "duration": duration,
        "video": pick("video"),
        "audio": pick("audio"),
        "format": fmt.get("format_name"),
    }


def check_media(report: Report, sec: str, row: sqlite3.Row, use_ffprobe: bool) -> dict[str, Any]:
    """video_path 存在性 + ffprobe。返回 {duration, ...} 供时间轴检查用。"""
    out: dict[str, Any] = {"video_path": row["video_path"], "duration": None}
    raw = (row["video_path"] or "").strip()
    if not raw:
        report.warn(sec, "未登记 video_path（/media 播放不了，仅能看字幕）")
        return out
    p = Path(raw)
    if not p.exists():
        report.fail(sec, f"video_path 失效（文件不存在）: {raw}", video_path=raw)
        return out
    size = p.stat().st_size
    out["video_size"] = size
    if size == 0:
        report.fail(sec, f"视频文件是空的: {raw}", video_path=raw)
        return out
    report.ok(sec, f"视频存在: {raw} ({_human_size(size)})")

    srt = (row["srt_path"] or "").strip()
    if srt and not Path(srt).exists():
        report.warn(sec, f"srt_path 失效（不影响播放，段已入库）: {srt}", srt_path=srt)

    if not use_ffprobe:
        report.warn(sec, "已跳过 ffprobe 探测（--no-ffprobe）")
        return out

    info = probe_video(p)
    out["probe"] = info
    if not info.get("ok"):
        msg = info.get("error", "未知错误")
        if info.get("skipped"):
            report.warn(sec, f"跳过 ffprobe: {msg}")
        else:
            report.fail(sec, f"ffprobe 读不出媒体信息: {msg}", error=msg)
        return out

    out["duration"] = info.get("duration")
    if not info["video"]:
        report.fail(sec, "视频里没有视频轨")
    else:
        v = info["video"][0]
        report.ok(
            sec,
            f"视频轨 {v['codec']} {v.get('width')}x{v.get('height')}"
            + (f"，时长 {info['duration']:.1f}s" if info.get("duration") else "，时长未知"),
            **v,
        )
    if not info["audio"]:
        report.warn(sec, "没有音频轨（跟读/听力用不了；合成测试片正常）")
    else:
        report.ok(sec, f"音频轨 {info['audio'][0]['codec']}")
    if info.get("duration") is None:
        report.warn(sec, "容器里读不到时长，时间轴越界检查降级为只查负值/倒挂")
    return out


# --- 3~5. 字幕段 / 词框 / tokens -------------------------------------------


def _load_json_list(raw: Any) -> tuple[list, str | None]:
    if raw is None:
        return [], None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError) as e:
        return [], f"JSON 解析失败: {e}"
    if not isinstance(val, list):
        return [], f"顶层应为列表，实际是 {type(val).__name__}"
    return val, None


def _box_surfaces(words: list) -> list[str]:
    """词框的 w 序列 → 可与 tokens.surface 逐项比较的归一序列。

    OCR 的 w 带标点（"raining,"）、可能是纯数字（"3"）或纯符号（"--"）——
    按 ingest 的同一套分词规则展开，纯数字/纯符号自然消失，与 tokenize() 对齐。
    """
    out: list[str] = []
    for w in words:
        text = w.get("w") if isinstance(w, dict) else None
        if not isinstance(text, str):
            continue
        for m in WORD_RE.finditer(text):
            s = normalize_surface(m.group(0))
            if s:
                out.append(s)
    return out


def _coord_issues(words: list) -> tuple[int, int, int, int]:
    """→ (空坐标数, 负坐标数, 越界数, 结构非法数)。"""
    null_xy = neg = oob = bad = 0
    for w in words:
        if not isinstance(w, dict) or "w" not in w:
            bad += 1
            continue
        if w.get("x") is None:
            null_xy += 1
            continue
        try:
            x, y = float(w["x"]), float(w["y"])
            width, height = float(w["width"]), float(w["height"])
        except (TypeError, ValueError, KeyError):
            bad += 1
            continue
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            neg += 1
            continue
        if x + width > REF_W or y + height > REF_H:
            oob += 1
    return null_xy, neg, oob, bad


def check_segments(
    conn: sqlite3.Connection,
    report: Report,
    sec: str,
    content_id: int,
    duration: float | None,
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT idx, t_start, t_end, text_en, tokens_json, word_boxes_json "
        "FROM Segment WHERE content_id = ? ORDER BY t_start, idx",
        (content_id,),
    ).fetchall()
    stats: dict[str, Any] = {"segments": len(rows)}
    if not rows:
        report.fail(sec, "一个字幕段都没有（ingest 没跑？）", segments=0)
        return stats

    # --- 时间轴 ---
    negative, inverted, overrun, overlap = [], [], [], []
    prev_end, prev_idx = None, None
    for r in rows:
        if r["t_start"] < 0:
            negative.append(r["idx"])
        if r["t_end"] <= r["t_start"]:
            inverted.append(r["idx"])
        if duration is not None and r["t_end"] > duration + DURATION_SLACK:
            overrun.append(r["idx"])
        if prev_end is not None and r["t_start"] < prev_end - OVERLAP_EPS:
            overlap.append((prev_idx, r["idx"]))
        prev_end, prev_idx = r["t_end"], r["idx"]

    span = f"{rows[0]['t_start']:.1f}s ~ {rows[-1]['t_end']:.1f}s"
    stats.update(
        timeline={
            "span_start": rows[0]["t_start"],
            "span_end": rows[-1]["t_end"],
            "negative_idx": negative,
            "inverted_idx": inverted,
            "overrun_idx": overrun,
            "overlap_pairs": [list(p) for p in overlap],
        }
    )
    if negative:
        report.fail(sec, f"{len(negative)} 段 t_start < 0: {negative[:MAX_EXAMPLES]}", idx=negative)
    if inverted:
        report.fail(
            sec, f"{len(inverted)} 段 t_end <= t_start: {inverted[:MAX_EXAMPLES]}", idx=inverted
        )
    if overrun:
        report.fail(
            sec,
            f"{len(overrun)} 段超出视频时长 {duration:.1f}s: {overrun[:MAX_EXAMPLES]}",
            idx=overrun,
            duration=duration,
        )
    if overlap:
        report.warn(
            sec,
            f"{len(overlap)} 处相邻段重叠（前段没结束后段就开始）: {overlap[:MAX_EXAMPLES]}",
            pairs=[list(p) for p in overlap],
        )
    if not (negative or inverted or overrun or overlap):
        report.ok(sec, f"{len(rows)} 段，时间轴自洽（{span}）", segments=len(rows))
    else:
        report.ok(sec, f"{len(rows)} 段（{span}）", segments=len(rows))

    # --- 词框 + tokens ---
    boxed = total_boxes = null_xy = neg = oob = bad_struct = 0
    no_tokens: list[int] = []
    broken_boxes: list[int] = []
    broken_tokens: list[int] = []
    count_diff: list[dict[str, Any]] = []
    text_diff: list[dict[str, Any]] = []
    total_tokens = 0

    for r in rows:
        idx = r["idx"]
        tokens, terr = _load_json_list(r["tokens_json"])
        if terr:
            broken_tokens.append(idx)
        elif r["tokens_json"] is None:
            no_tokens.append(idx)
        tok_seq = [
            t.get("surface", "") for t in tokens if isinstance(t, dict)
        ]
        total_tokens += len(tok_seq)

        if r["word_boxes_json"] is None:
            continue
        words, berr = _load_json_list(r["word_boxes_json"])
        if berr:
            broken_boxes.append(idx)
            continue
        if not words:
            continue
        boxed += 1
        total_boxes += len(words)
        a, b, c, d = _coord_issues(words)
        null_xy, neg, oob, bad_struct = null_xy + a, neg + b, oob + c, bad_struct + d

        if r["tokens_json"] is None or terr:
            continue
        box_seq = _box_surfaces(words)
        # 比的是「可点击词」的个数：词框里的纯数字/纯符号（"3"、"--"）本来就不成 token，
        # 拿原始框数去比会在任何带数字的句子上误报。原始框数另存 boxes_raw。
        if len(box_seq) != len(tok_seq):
            count_diff.append(
                {"idx": idx, "tokens": len(tok_seq), "boxes": len(box_seq),
                 "boxes_raw": len(words)}
            )
        if box_seq != tok_seq:
            text_diff.append(
                {
                    "idx": idx,
                    "tokens": tok_seq[:8],
                    "boxes": box_seq[:8],
                }
            )

    coverage = boxed / len(rows)
    stats.update(
        boxes={
            "segments_with_boxes": boxed,
            "coverage": round(coverage, 4),
            "total_boxes": total_boxes,
            "null_coords": null_xy,
            "negative_coords": neg,
            "out_of_range_coords": oob,
            "malformed_entries": bad_struct,
            "unparsable_idx": broken_boxes,
            "reference_frame": [REF_W, REF_H],
        },
        tokens={
            "total_tokens": total_tokens,
            "segments_without_tokens": no_tokens,
            "unparsable_idx": broken_tokens,
            "count_mismatch": count_diff,
            "text_mismatch": text_diff,
        },
    )

    if broken_tokens:
        report.fail(
            sec,
            f"{len(broken_tokens)} 段 tokens_json 解析不了: {broken_tokens[:MAX_EXAMPLES]}",
            idx=broken_tokens,
        )
    if no_tokens:
        report.warn(
            sec,
            f"{len(no_tokens)} 段没有 tokens_json（点词查不了）: {no_tokens[:MAX_EXAMPLES]}",
            idx=no_tokens,
        )
    if broken_boxes:
        report.fail(
            sec,
            f"{len(broken_boxes)} 段 word_boxes_json 解析不了: {broken_boxes[:MAX_EXAMPLES]}",
            idx=broken_boxes,
        )

    cov_msg = f"词框覆盖 {boxed}/{len(rows)} 段（{coverage:.0%}），共 {total_boxes} 个词框"
    if boxed == 0:
        report.warn(sec, "没有任何词框，播放器退回自渲染热区" + f"（{len(rows)} 段）")
    elif coverage < COVERAGE_WARN_RATIO:
        report.warn(sec, cov_msg + "，其余段走自渲染退路", coverage=coverage)
    else:
        report.ok(sec, cov_msg, coverage=coverage)

    if total_boxes:
        ratio = null_xy / total_boxes
        if ratio > NULL_BOX_WARN_RATIO:
            report.warn(
                sec,
                f"空坐标（OCR 丢框）{null_xy}/{total_boxes} = {ratio:.1%}，超过 {NULL_BOX_WARN_RATIO:.0%}",
                null_coords=null_xy,
            )
        elif null_xy:
            report.ok(
                sec,
                f"空坐标 {null_xy}/{total_boxes}（{ratio:.1%}，在容忍范围内，前端跳过这些词的热区）",
                null_coords=null_xy,
            )
        else:
            report.ok(sec, "词框坐标无空值", null_coords=0)
        if neg:
            report.warn(sec, f"{neg} 个词框坐标为负或宽高非正", negative_coords=neg)
        if oob:
            report.warn(
                sec,
                f"{oob} 个词框超出 {REF_W}x{REF_H} 参考系（源分辨率不是 1080p？）",
                out_of_range=oob,
            )
        if bad_struct:
            report.fail(sec, f"{bad_struct} 个词框结构非法（缺 w / 坐标非数字）", malformed=bad_struct)

    if boxed:
        if count_diff:
            ex = "、".join(f"#{d['idx']}({d['tokens']}词/{d['boxes']}框)" for d in count_diff[:MAX_EXAMPLES])
            report.warn(
                sec, f"{len(count_diff)}/{boxed} 段 tokens 与词框数量不一致: {ex}", segments=count_diff[:MAX_EXAMPLES]
            )
        misalign = len(text_diff) / boxed
        if text_diff and misalign > MISALIGN_FAIL_RATIO:
            report.fail(
                sec,
                f"{len(text_diff)}/{boxed} 段 tokens 与词框文本对不上（{misalign:.0%}），"
                f"boxes 很可能不是这一集的产物；例: {text_diff[0]}",
                segments=text_diff[:MAX_EXAMPLES],
            )
        elif text_diff:
            report.warn(
                sec,
                f"{len(text_diff)}/{boxed} 段 tokens 与词框文本有出入; 例: {text_diff[0]}",
                segments=text_diff[:MAX_EXAMPLES],
            )
        else:
            report.ok(sec, f"{boxed} 段 tokens 与词框逐词对齐", segments=boxed)
    return stats


# --- 6. ECDICT -------------------------------------------------------------


def check_ecdict(report: Report, path: Path) -> dict[str, Any]:
    sec = "ecdict"
    out: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    report.data["ecdict"] = out
    if not path.exists():
        report.warn(sec, f"词典不存在: {path}（查词降级为 in_dict=false，跑 scripts/build_ecdict.py）")
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        entries = _scalar(conn, "SELECT COUNT(*) FROM ecdict")
    except sqlite3.Error as e:
        report.fail(sec, f"词典打不开或没有 ecdict 表: {path}: {e}", error=str(e))
        return out
    out["entries"] = entries
    words = [
        r["word_lower"]
        for r in conn.execute(
            "SELECT word_lower FROM ecdict ORDER BY id LIMIT ?", (ECDICT_SAMPLE,)
        )
    ]
    conn.close()
    if entries == 0:
        report.fail(sec, f"词典是空的（0 条）: {path}", entries=0)
        return out
    report.ok(sec, f"词典可用: {path}（{entries} 条，{_human_size(path.stat().st_size)}）", entries=entries)

    store = EcdictStore(path)
    hit = [w for w in words if store.lookup(w) is not None]
    store.close_all()
    out["sample"] = {"tried": words, "hit": len(hit)}
    if len(hit) != len(words):
        miss = [w for w in words if w not in hit]
        report.fail(
            sec,
            f"抽样查询 {len(hit)}/{len(words)} 命中，查不到: {miss}（word_lower 列/索引坏了？）",
            missed=miss,
        )
    else:
        report.ok(sec, f"抽样查询 {len(hit)}/{len(words)} 命中（{', '.join(words[:3])}…）")
    return out


# --- 7. 汇总 + 孤儿外键 ----------------------------------------------------

# (子表, 外键列, 父表, 父键) —— PRAGMA foreign_key_check 之外再自己点一遍，
# 这样能报出「哪张表哪个列有几行悬空」，而不是一坨行号。
FK_LINKS = (
    ("Segment", "content_id", "Content", "id"),
    ("WordForm", "lexeme_id", "Lexeme", "id"),
    ("VocabEntry", "lexeme_id", "Lexeme", "id"),
    ("Encounter", "vocab_entry_id", "VocabEntry", "id"),
    ("Encounter", "segment_id", "Segment", "id"),
    ("AnnotationJob", "lexeme_id", "Lexeme", "id"),
    ("Mnemonic", "lexeme_id", "Lexeme", "id"),
    ("Review", "vocab_entry_id", "VocabEntry", "id"),
)


def check_summary(conn: sqlite3.Connection, report: Report) -> dict[str, Any]:
    sec = "汇总"
    counts = {t: _scalar(conn, f"SELECT COUNT(*) FROM {t}") for t in TABLES}
    report.data["counts"] = counts
    report.ok(
        sec,
        "行数: "
        + ", ".join(
            f"{t}={counts[t]}"
            for t in ("Content", "Segment", "Lexeme", "VocabEntry", "Encounter",
                      "AnnotationJob", "Mnemonic", "Review")
        ),
        **counts,
    )

    orphans: dict[str, int] = {}
    for child, col, parent, pk in FK_LINKS:
        n = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {child} c WHERE c.{col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{pk} = c.{col})",
        )
        if n:
            orphans[f"{child}.{col}->{parent}"] = n
    fk_check = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    report.data["orphans"] = {"by_link": orphans, "pragma_foreign_key_check": fk_check}
    if orphans or fk_check:
        detail = ", ".join(f"{k}: {v} 行" for k, v in orphans.items()) or f"{fk_check} 行"
        report.fail(sec, f"孤儿外键（引用了不存在的行）: {detail}", orphans=orphans, fk_check=fk_check)
    else:
        report.ok(sec, "外键自洽，无孤儿行")

    # web 来源的 Encounter 必须有 context_json，否则 /vocab 里那一行是个哑巴。
    # v1 老库根本没这两列：跳过并说明，绝不硬查（工单 17-3——硬查就是 traceback）。
    gap = encounter_v2_gap(conn)
    if gap:
        report.warn(
            sec,
            f"跳过 web 来源 Encounter 的语境检查：老库缺 {', '.join(gap)} 列（见 schema 一节）",
            skipped="encounter_context_json",
            encounter_missing=gap,
        )
        return counts
    bad_web = _scalar(
        conn,
        "SELECT COUNT(*) FROM Encounter WHERE source_kind='web' AND "
        "(context_json IS NULL OR context_json = '')",
    )
    if bad_web:
        report.warn(sec, f"{bad_web} 条 web 来源的 Encounter 没有 context_json（语境丢了）",
                    count=bad_web)
    return counts


# --- 编排 ------------------------------------------------------------------


def run_doctor(
    db_path: str | Path = DEFAULT_DB,
    content_id: int | None = None,
    ecdict_path: str | Path = DEFAULT_ECDICT,
    use_ffprobe: bool = True,
) -> Report:
    """跑一遍体检，返回 Report。全程只读。"""
    report = Report()
    db = Path(db_path)
    report.data["db"] = {"path": str(db), "exists": db.exists()}
    try:
        conn = open_readonly(db)
    except DoctorError as e:
        report.fail("db", str(e), path=str(db))
        return report
    report.data["db"]["size"] = db.stat().st_size
    report.ok("db", f"打开成功（只读）: {db}（{_human_size(db.stat().st_size)}）")

    try:
        if not check_schema(conn, report):
            return report

        if content_id is None:
            rows = conn.execute(
                "SELECT * FROM Content ORDER BY title, season_ep"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM Content WHERE id = ?", (content_id,)
            ).fetchall()
            if not rows:
                report.fail("content", f"content_id={content_id} 不存在", content_id=content_id)
                return report
        if not rows:
            report.warn("content", "库里一集内容都没有（先跑 python -m app.ingest）")

        contents = []
        for row in rows:
            sec = f"{row['title']} {row['season_ep']} (content_id={row['id']})"
            media = check_media(report, sec, row, use_ffprobe)
            seg = check_segments(conn, report, sec, int(row["id"]), media.get("duration"))
            contents.append(
                {
                    "content_id": int(row["id"]),
                    "title": row["title"],
                    "season_ep": row["season_ep"],
                    "media": media,
                    **seg,
                }
            )
        report.data["contents"] = contents

        check_ecdict(report, Path(ecdict_path))
        check_summary(conn, report)
    finally:
        conn.close()
    return report


# --- 输出 ------------------------------------------------------------------


def render(report: Report, strict: bool = False) -> str:
    lines: list[str] = []
    for section, findings in report.sections():
        lines.append(f"\n== {section} ==")
        for f in findings:
            lines.append(f"  {MARK[f.level]} {f.message}")
    c = report.counts()
    v = report.verdict
    if v == FAIL:
        tail = f"{MARK[FAIL]} 体检不通过：{c[FAIL]} 项失败, {c[WARN]} 项警告, {c[OK]} 项通过"
    elif v == WARN:
        note = "（--strict 下按失败处理）" if strict else ""
        tail = f"{MARK[WARN]} 可用但有隐患：{c[WARN]} 项警告, {c[OK]} 项通过{note}"
    else:
        tail = f"{MARK[OK]} 全部通过：{c[OK]} 项"
    lines.append(f"\nverdict: {tail}")
    return "\n".join(lines).lstrip("\n")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.doctor",
        description="只读体检：视频/时间轴/词框/tokens/词典/外键",
    )
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite 路径")
    ap.add_argument("--content-id", type=int, default=None, help="只查这一集")
    ap.add_argument("--ecdict", default=DEFAULT_ECDICT, help="ECDICT 路径")
    ap.add_argument("--json", action="store_true", help="输出机器可读完整报告")
    ap.add_argument("--strict", action="store_true", help="⚠ 也算失败（退出码非零）")
    ap.add_argument(
        "--no-ffprobe", dest="ffprobe", action="store_false", help="跳过 ffprobe 媒体探测"
    )
    args = ap.parse_args(argv)

    report = run_doctor(
        db_path=args.db,
        content_id=args.content_id,
        ecdict_path=args.ecdict,
        use_ffprobe=args.ffprobe,
    )
    if args.json:
        print(json.dumps(report.to_json(args.strict), ensure_ascii=False, indent=2))
    else:
        print(render(report, args.strict))
    return report.exit_code(args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
