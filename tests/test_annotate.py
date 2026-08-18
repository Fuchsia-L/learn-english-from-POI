"""app/annotate.py：worker 全链路、预算截断、重试/失败、用户编辑保护。

夹具一律自造（复用 conftest 的 FIXTURE_SRT + build_ecdict 的 mini 词典），
provider 一律 fake —— **本文件不发任何真实网络请求**，跑一次成本 ¥0。
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app import annotate as A  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.ingest import ingest_srt  # noqa: E402
from app.providers.fake import FakeProvider  # noqa: E402
from app.server import create_app  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    srt = tmp_path / "fixture.srt"
    srt.write_text(FIXTURE_SRT, encoding="utf-8")
    ecdict = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)
    db = tmp_path / "poi.db"
    stats = ingest_srt(
        db_path=db, srt_path=srt, title="Test Show", season_ep="s01e01"
    )
    return {"db": db, "ecdict": ecdict, "content_id": stats["content_id"]}


@pytest.fixture()
def client(env: dict):
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def logs() -> list[str]:
    return []


def make_worker(env: dict, provider=None, logs=None, **kw) -> A.AnnotateWorker:
    return A.AnnotateWorker(
        db_path=env["db"],
        provider=provider or FakeProvider(),
        ecdict_path=env["ecdict"],
        log=(logs.append if logs is not None else lambda _m: None),
        **kw,
    )


def seg_id(client: TestClient, content_id: int, idx: int = 0) -> int:
    segs = client.get("/segments", params={"content_id": content_id}).json()["segments"]
    return segs[idx]["id"]


def collect(client: TestClient, surface: str, segment_id: int) -> dict:
    r = client.post("/collect", json={"surface": surface, "segment_id": segment_id})
    assert r.status_code == 200
    return r.json()


def enqueue(db: Path, lemma: str, priority: int = 0) -> int:
    """直接排一个低优先级任务（模拟 prefetch 的产物）。"""
    conn = get_conn(db)
    with conn:
        lex = conn.execute("SELECT id FROM Lexeme WHERE lemma = ?", (lemma,)).fetchone()
        assert lex is not None, f"{lemma} 不在这一集里"
        cur = conn.execute(
            "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
            "VALUES (?, 'queued', ?, '2026-01-01T00:00:00+00:00')",
            (int(lex["id"]), priority),
        )
        job_id = int(cur.lastrowid)
    conn.close()
    return job_id


def job_status(db: Path, job_id: int) -> sqlite3.Row:
    conn = get_conn(db)
    row = conn.execute(
        "SELECT status, done_at FROM AnnotationJob WHERE id = ?", (job_id,)
    ).fetchone()
    conn.close()
    return row


class BrokenEstimate:
    """估价坏掉的 provider（工单 8b 的靶子）。

    annotate() 一旦被调用就是 bug —— 估价不可用时一次真实调用都不该发出去。
    """

    name = "broken-estimate"

    def __init__(self, value=0.0, raises: Exception | None = None) -> None:
        self.value = value
        self.raises = raises
        self.calls = 0

    def annotate(self, batch):  # pragma: no cover - 被调用即测试失败
        self.calls += 1
        raise AssertionError("估价不可用时绝不许调 provider（会真花钱）")

    def estimate_cost(self, batch):
        if self.raises is not None:
            raise self.raises
        return self.value


def mnemonic_rows(db: Path, lexeme_id: int) -> list[sqlite3.Row]:
    conn = get_conn(db)
    rows = conn.execute(
        "SELECT kind, payload_json, provider, version, edited_by_user FROM Mnemonic "
        "WHERE lexeme_id = ? ORDER BY version, kind",
        (lexeme_id,),
    ).fetchall()
    conn.close()
    return rows


# --- 全链路 ----------------------------------------------------------------


def test_collect_to_worker_to_mnemonic_endpoint(client: TestClient, env: dict, monkeypatch):
    """DESIGN §5 主链路：收藏入队 → worker --once → /mnemonic 返回 done 内容。

    顺带把 socket 打成地雷：全链路（fake provider）不许碰网络。
    """

    class Boom(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError("fake provider 全链路不应发起任何网络调用")

    monkeypatch.setattr(socket, "socket", Boom)

    r = collect(client, "cameras", seg_id(client, env["content_id"], idx=2))
    lexeme_id = r["lexeme_id"]
    assert client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()["status"] == "queued"

    rc = A.main(
        ["--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
         "--provider", "fake", "--once", "--quiet"]
    )
    assert rc == 0

    body = client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()
    assert body["status"] == "done"
    assert body["job"]["status"] == "done" and body["job"]["done_at"]
    kinds = {m["kind"]: m for m in body["mnemonics"]}
    assert "gloss" in kinds and "morph" in kinds
    gloss = kinds["gloss"]["payload"]["text"]
    assert gloss.startswith("〔fake〕")
    assert "My cousins bought tw" in gloss  # 收藏那一句的原句进了输入包（前 20 字）
    assert kinds["gloss"]["provider"] == "fake" and kinds["gloss"]["version"] == 1
    assert "核验" in kinds["morph"]["payload"]["label"]   # morph 永远标未经核验
    # /vocab 侧也看得到
    entry = next(v for v in client.get("/vocab").json()["vocab"] if v["lexeme_id"] == lexeme_id)
    assert entry["has_mnemonic"] and entry["mnemonic_status"] == "done"


def test_pack_contains_design_fields(client: TestClient, env: dict):
    collect(client, "cameras", seg_id(client, env["content_id"], idx=2))
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
    pack = p.calls[0][0]
    assert set(pack) == {
        "id", "lemma", "surface", "pos", "ipa", "dict_gloss",
        "sentence", "speaker", "episode", "t",
    }
    assert pack["id"].isdigit()  # 对位键 = AnnotationJob.id（工单 6-2）
    assert pack["lemma"] == "camera" and pack["surface"] == "cameras"
    assert pack["episode"] == "s01e01" and pack["speaker"] is None
    assert pack["t"] == pytest.approx(8.0)


def test_prefetch_word_without_encounter_uses_episode_sentence(env: dict):
    """预热词没有 encounter：退回"当集任一含该词的 Segment 原句"（工单 4）。"""
    enqueue(env["db"], "gardener", priority=0)
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        stats = w.run_once()
    assert stats.done == 1
    pack = p.calls[0][0]
    assert pack["lemma"] == "gardener"
    assert pack["sentence"] == "The tall gardener went home early."
    assert pack["episode"] == "s01e01"


def collect_web(client: TestClient, surface: str, sentence: str) -> dict:
    r = client.post(
        "/collect/web",
        json={
            "surface": surface,
            "sentence": sentence,
            "url": "https://example.invalid/a",
            "title": "Example page",
        },
    )
    assert r.status_code == 200
    return r.json()


def test_pack_uses_web_encounter_sentence(client: TestClient, env: dict):
    """网页划词收藏（工单 11）：原句用网页那句，episode 写 "web"，没有时间戳。"""
    sentence = "The tired gardener began a stakeout near the greenhouse door."
    collect_web(client, "stakeouts", sentence)
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        stats = w.run_once()
    assert stats.done == 1
    pack = p.calls[0][0]
    assert pack["lemma"] == "stakeout" and pack["surface"] == "stakeouts"
    assert pack["sentence"] == sentence
    assert pack["episode"] == "web"
    assert pack["t"] is None


def test_pack_prefers_latest_encounter_with_a_sentence(client: TestClient, env: dict):
    """网页收藏偶尔截不到句子；那条不该把上一次有原句的语境挤掉。"""
    sentence = "Two cameras watched the empty office all night."
    collect_web(client, "cameras", sentence)
    r = client.post("/collect/web", json={"surface": "camera"})  # 没有 sentence
    assert r.status_code == 200
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
    pack = p.calls[0][0]
    assert pack["sentence"] == sentence and pack["episode"] == "web"


def test_pack_web_encounter_beats_subtitle_when_newer(client: TestClient, env: dict):
    """两种来源都有：仍按"最近一条"取，网页那条在后就用网页那条。"""
    collect(client, "cameras", seg_id(client, env["content_id"], idx=2))
    web_sentence = "The cameras in the hallway were never switched on."
    collect_web(client, "cameras", web_sentence)
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
    pack = p.calls[0][0]
    assert pack["sentence"] == web_sentence and pack["episode"] == "web"


def test_ecdict_fields_backfilled_into_lexeme(env: dict):
    """Lexeme 骨架行（ingest 只写 lemma）在组包时被 ECDICT 补齐并回写缓存。"""
    enqueue(env["db"], "cousin", priority=0)
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
    pack = p.calls[0][0]
    assert pack["ipa"] == "ˈkʌzn" and "堂表兄弟姐妹" in pack["dict_gloss"]
    conn = get_conn(env["db"])
    row = conn.execute("SELECT ipa, dict_gloss FROM Lexeme WHERE lemma='cousin'").fetchone()
    conn.close()
    assert row["ipa"] == "ˈkʌzn" and row["dict_gloss"]  # 回填进了 Lexeme 缓存


def test_lemma_absent_from_episode_still_annotated(env: dict):
    """既无 encounter 又不在字幕里的词：裸词照样送模型，不算失败。"""
    conn = get_conn(env["db"])
    with conn:
        cur = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
        conn.execute(
            "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
            "VALUES (?, 'queued', 0, '2026-01-01T00:00:00+00:00')",
            (int(cur.lastrowid),),
        )
    conn.close()
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        stats = w.run_once()
    assert stats.done == 1 and p.calls[0][0]["sentence"] is None


# --- 取任务顺序 -------------------------------------------------------------


def test_priority_desc_then_id_asc(client: TestClient, env: dict):
    enqueue(env["db"], "gardener", priority=0)
    enqueue(env["db"], "cheap", priority=0)
    collect(client, "cameras", seg_id(client, env["content_id"], idx=2))  # priority=10
    p = FakeProvider()
    with make_worker(env, provider=p, batch_size=1) as w:
        w.run_once()
    order = [c[0]["lemma"] for c in p.calls]
    assert order == ["camera", "gardener", "cheap"]


# --- 预算制（DESIGN §5） ---------------------------------------------------


def test_budget_truncates_low_priority(env: dict, logs: list):
    for lemma in ("gardener", "cheap", "nobody", "street"):
        enqueue(env["db"], lemma, priority=0)
    p = FakeProvider(cost_per_item=1.0)
    with make_worker(env, provider=p, batch_size=1, budget=2.0, logs=logs) as w:
        stats = w.run_once()
    assert stats.done == 2 and stats.skipped_budget == 2
    assert stats.spent == pytest.approx(2.0)
    assert any("预算已用尽" in m for m in logs)

    conn = get_conn(env["db"])
    left = conn.execute(
        "SELECT COUNT(*) c FROM AnnotationJob WHERE status = 'queued'"
    ).fetchone()["c"]
    conn.close()
    assert left == 2  # 被截断的任务保持 queued，不是 failed


def test_budget_can_be_resumed_by_raising_it(env: dict):
    for lemma in ("gardener", "cheap"):
        enqueue(env["db"], lemma, priority=0)
    with make_worker(env, provider=FakeProvider(cost_per_item=1.0),
                     batch_size=1, budget=1.0) as w:
        assert w.run_once().done == 1
    with make_worker(env, provider=FakeProvider(cost_per_item=1.0),
                     batch_size=1, budget=5.0) as w:
        assert w.run_once().done == 1


def test_high_priority_ignores_budget(client: TestClient, env: dict):
    """收藏的词永远不受预算限制（DESIGN §5：点击收藏的词永远高优先级插队）。"""
    collect(client, "cameras", seg_id(client, env["content_id"], idx=2))
    enqueue(env["db"], "gardener", priority=0)
    p = FakeProvider(cost_per_item=99.0)
    with make_worker(env, provider=p, batch_size=1, budget=0.0) as w:
        stats = w.run_once()
    assert stats.done == 1 and stats.skipped_budget == 1
    assert [c[0]["lemma"] for c in p.calls] == ["camera"]
    # 不受限 ≠ 不记账：高优先级的花费照样进 est_cost（工单 6-1）
    assert stats.est_cost == pytest.approx(99.0)


def test_budget_is_not_blown_by_retries(env: dict, logs: list):
    """工单 6-1：重试也要先过预算关。

    老实现每批只在入口记一次账：4 个任务 × 每次调用 ¥1、retries=2 时，
    记账只算 ¥4，实际却调了 12 次 = ¥12。现在每次真实调用前记账 + 校预算，
    总花费不可能越过「预算 + 最后一次调用」这条线。
    """
    for lemma in ("gardener", "cheap", "nobody", "street"):
        enqueue(env["db"], lemma, priority=0)
    p = FakeProvider(cost_per_item=1.0, fail_on=("gardener", "cheap", "nobody", "street"))
    with make_worker(
        env, provider=p, batch_size=1, budget=4.0, retries=2, logs=logs
    ) as w:
        stats = w.run_once()

    per_call_max = 1.0  # batch_size=1 × cost_per_item
    assert stats.calls == 4                       # 老实现在这里会调 12 次
    assert len(p.calls) == stats.calls            # provider 侧的实际调用次数一致
    assert stats.est_cost == pytest.approx(4.0)
    assert stats.est_cost <= 4.0 + per_call_max   # 预算 + 单次调用上限
    assert stats.skipped_budget == 3 and stats.failed == 1
    assert any("预算已用尽" in m for m in logs)

    conn = get_conn(env["db"])
    left = conn.execute(
        "SELECT COUNT(*) c FROM AnnotationJob WHERE status = 'queued'"
    ).fetchone()["c"]
    conn.close()
    assert left == 3  # 没送出去的（含被预算掐掉的那批）全部保持 queued


def test_every_retry_is_charged(env: dict):
    """重试计费：失败一次再成功 = 两次调用 = 两次记账。"""
    enqueue(env["db"], "gardener")
    p = FakeProvider(cost_per_item=0.5, fail_on="gardener", fail_times=1)
    with make_worker(env, provider=p, batch_size=1, budget=10.0, retries=2) as w:
        stats = w.run_once()
    assert stats.done == 1 and stats.calls == 2
    assert stats.est_cost == pytest.approx(1.0)  # 0.5 × 2 次调用
    assert stats.as_dict()["est_cost"] == pytest.approx(1.0)


def test_estimate_broken_stops_the_round_and_keeps_jobs_queued(env: dict, logs: list):
    """工单 8b：估价坏掉 → 停手，绝不按 ¥0 继续跑。"""
    for lemma in ("gardener", "cheap", "nobody"):
        enqueue(env["db"], lemma, priority=0)
    p = BrokenEstimate(raises=RuntimeError("估价服务 500"))
    with make_worker(env, provider=p, batch_size=1, budget=100.0, logs=logs) as w:
        stats = w.run_once()

    assert p.calls == 0                    # 一次 provider 调用都没发
    assert stats.calls == 0
    assert stats.done == 0 and stats.failed == 0
    assert stats.est_cost == 0.0           # 不是"按 0 记账继续跑"，是根本没跑
    assert stats.estimate_broken is True
    assert stats.skipped_estimate == 3
    assert any("估价不可用" in m for m in logs)

    conn = get_conn(env["db"])
    left = conn.execute(
        "SELECT COUNT(*) c FROM AnnotationJob WHERE status = 'queued'"
    ).fetchone()["c"]
    conn.close()
    assert left == 3                       # 全部保持 queued，等人来修


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raises": RuntimeError("boom")},
        {"raises": ValueError("坏掉了")},
        {"value": None},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": -1.0},
        {"value": "免费啦"},
    ],
    ids=["raise", "raise-value", "none", "nan", "inf", "negative", "not-a-number"],
)
def test_estimate_bad_values_all_fail_closed(env: dict, kwargs, logs: list):
    """异常 / None / NaN / inf / 负数 / 非数字，一律停手，不许当 ¥0。"""
    job = enqueue(env["db"], "gardener", priority=0)
    p = BrokenEstimate(**kwargs)
    with make_worker(env, provider=p, batch_size=1, budget=100.0, logs=logs) as w:
        stats = w.run_once()
    assert p.calls == 0 and stats.calls == 0 and stats.est_cost == 0.0
    assert stats.estimate_broken is True and stats.skipped_estimate == 1
    assert job_status(env["db"], job)["status"] == "queued"


def test_estimate_broken_stops_high_priority_too(client: TestClient, env: dict, logs: list):
    """估价系统坏了就是坏了：点击收藏的高优先级任务同样一分钱不花（工单 8b）。"""
    collect(client, "cameras", seg_id(client, env["content_id"], idx=2))
    enqueue(env["db"], "gardener", priority=0)
    p = BrokenEstimate(raises=RuntimeError("估价服务 500"))
    with make_worker(env, provider=p, batch_size=1, budget=100.0, logs=logs) as w:
        stats = w.run_once()

    assert p.calls == 0 and stats.calls == 0 and stats.done == 0 and stats.failed == 0
    assert stats.estimate_broken is True and stats.skipped_estimate == 2
    conn = get_conn(env["db"])
    rows = conn.execute("SELECT status FROM AnnotationJob").fetchall()
    conn.close()
    assert [r["status"] for r in rows] == ["queued", "queued"]
    assert any("估价不可用" in m for m in logs)


def test_estimate_broken_exits_loop_instead_of_spinning(env: dict):
    """--loop 遇到估价坏掉不该无限空转刷屏，直接退出让人来修。"""
    enqueue(env["db"], "gardener", priority=0)
    p = BrokenEstimate(raises=RuntimeError("boom"))
    with make_worker(env, provider=p, batch_size=1) as w:
        total = w.run_loop(poll=0.0, max_rounds=50)
    assert total.estimate_broken is True and total.batches == 1 and p.calls == 0


def test_cli_returns_exit_code_3_when_estimate_broken(env: dict, monkeypatch, capsys):
    monkeypatch.setattr(
        A, "build_provider", lambda *_a, **_k: BrokenEstimate(raises=RuntimeError("x"))
    )
    enqueue(env["db"], "gardener", priority=0)
    rc = A.main(["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--once"])
    out = capsys.readouterr().out
    assert rc == A.EXIT_ESTIMATE_BROKEN == 3
    assert "估价不可用" in out and "skipped_estimate=1" in out


def test_dry_run_reports_estimate_failure_instead_of_zero(env: dict, monkeypatch, capsys):
    """dry-run 也不许报一个假的 ¥0：说不知道就是不知道。"""
    monkeypatch.setattr(
        A, "build_provider", lambda *_a, **_k: BrokenEstimate(value=float("nan"))
    )
    enqueue(env["db"], "gardener", priority=0)
    rc = A.main(
        ["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--dry-run"]
    )
    out = capsys.readouterr().out
    assert rc == A.EXIT_ESTIMATE_BROKEN
    assert "估价不可用" in out and "¥0" not in out


def test_budget_stops_mid_batch_retry_and_requeues(env: dict, logs: list):
    """预算在重试中途用尽：该任务放回 queued，不是 failed。"""
    job = enqueue(env["db"], "gardener")
    p = FakeProvider(cost_per_item=1.0, fail_on="gardener")
    with make_worker(
        env, provider=p, batch_size=1, budget=2.0, retries=5, logs=logs
    ) as w:
        stats = w.run_once()
    assert stats.calls == 2 and stats.est_cost == pytest.approx(2.0)
    assert stats.failed == 0 and stats.skipped_budget == 1
    assert job_status(env["db"], job)["status"] == "queued"


# --- 重试 / 失败 ------------------------------------------------------------


def test_retry_then_success(env: dict, logs: list):
    job = enqueue(env["db"], "gardener")
    p = FakeProvider(fail_on="gardener", fail_times=1)
    with make_worker(env, provider=p, retries=2, logs=logs) as w:
        stats = w.run_once()
    assert stats.done == 1 and stats.calls == 2
    assert job_status(env["db"], job)["status"] == "done"
    assert any("重试第 1/2 次" in m for m in logs)


def test_failed_after_two_retries(env: dict):
    job = enqueue(env["db"], "gardener")
    p = FakeProvider(fail_on="gardener")  # 永远失败
    with make_worker(env, provider=p, retries=2) as w:
        stats = w.run_once()
    assert stats.calls == 3  # 1 次 + 重试 2 次
    assert stats.failed == 1 and stats.done == 0
    row = job_status(env["db"], job)
    assert row["status"] == "failed" and row["done_at"]


def test_schema_violation_rejects_and_fails_job(env: dict, logs: list):
    """畸形输出必须被 schema 拦住：重试用尽 → failed，一行 Mnemonic 都不许落库。"""
    job = enqueue(env["db"], "gardener")
    p = FakeProvider(bad_output_on="gardener")
    with make_worker(env, provider=p, retries=2, logs=logs) as w:
        stats = w.run_once()
    assert stats.calls == 3 and stats.failed == 1 and stats.rows == 0
    assert job_status(env["db"], job)["status"] == "failed"
    assert any("schema" in m for m in logs)
    conn = get_conn(env["db"])
    assert conn.execute("SELECT COUNT(*) c FROM Mnemonic").fetchone()["c"] == 0
    conn.close()


def test_one_failure_does_not_affect_others(env: dict):
    """单任务失败不影响其余（工单 4 健壮性要求）。"""
    for lemma in ("gardener", "cheap", "nobody"):
        enqueue(env["db"], lemma)
    p = FakeProvider(fail_on="cheap")
    with make_worker(env, provider=p, batch_size=1, retries=1) as w:
        stats = w.run_once()
    assert stats.done == 2 and stats.failed == 1
    conn = get_conn(env["db"])
    got = {
        r["lemma"]: r["status"]
        for r in conn.execute(
            "SELECT L.lemma, J.status FROM AnnotationJob J "
            "JOIN Lexeme L ON L.id = J.lexeme_id"
        )
    }
    conn.close()
    assert got == {"gardener": "done", "cheap": "failed", "nobody": "done"}


def test_worker_survives_arbitrary_provider_exception(env: dict):
    class Exploding:
        name = "boom"

        def annotate(self, batch):
            raise RuntimeError("provider 内部爆炸")

        def estimate_cost(self, batch):
            return 0.0  # 估价正常，炸的是 annotate（估价坏掉另见 fail-closed 用例）

    enqueue(env["db"], "gardener")
    with make_worker(env, provider=Exploding(), retries=1) as w:
        stats = w.run_once()  # 不许把异常抛给调用方
    assert stats.failed == 1 and stats.spent == 0.0


def test_provider_not_configured_fails_fast_without_retry(env: dict, logs: list):
    from app.providers import ProviderNotConfigured

    class NoKey:
        name = "nokey"

        def annotate(self, batch):
            raise ProviderNotConfigured("缺 DEEPSEEK_API_KEY")

        def estimate_cost(self, batch):
            return 0.0

    enqueue(env["db"], "gardener")
    with make_worker(env, provider=NoKey(), retries=2, logs=logs) as w:
        stats = w.run_once()
    assert stats.calls == 1 and stats.failed == 1  # 缺 key 不重试
    assert any("未配置" in m for m in logs)


def test_wrong_output_count_is_rejected(env: dict):
    class Short:
        name = "short"

        def annotate(self, batch):
            return [{"context_gloss": "g", "hooks": []}]  # 少一条

        def estimate_cost(self, batch):
            return 0.0

    for lemma in ("gardener", "cheap"):
        enqueue(env["db"], lemma)
    with make_worker(env, provider=Short(), batch_size=2, retries=1) as w:
        stats = w.run_once()
    assert stats.failed == 2 and stats.rows == 0


# --- 按 id 对位（工单 6-2：批量助记不许串词） ------------------------------


def lexeme_id_of(db: Path, lemma: str) -> int:
    conn = get_conn(db)
    row = conn.execute("SELECT id FROM Lexeme WHERE lemma = ?", (lemma,)).fetchone()
    conn.close()
    assert row is not None
    return int(row["id"])


def marked_hooks(item: dict) -> list[dict]:
    """把 lemma 写进 hook 文本，落库后一眼看出这条助记原本属于谁。"""
    return [
        {
            "type": "morph",
            "text": f"这条属于 {item['lemma']}",
            "label": "拆分助记，未经词源核验",
        }
    ]


def hook_texts(db: Path, lemma: str) -> list[str]:
    return [
        json.loads(r["payload_json"]).get("text", "")
        for r in mnemonic_rows(db, lexeme_id_of(db, lemma))
    ]


def test_shuffled_provider_output_still_lands_on_the_right_lexeme(env: dict):
    """模型把数组顺序打乱：按 id 对位，助记必须还落在自己那个 lexeme 上。"""
    for lemma in ("gardener", "cheap", "nobody"):
        enqueue(env["db"], lemma, priority=0)
    p = FakeProvider(shuffle=True, hooks_for=marked_hooks)
    with make_worker(env, provider=p, batch_size=3) as w:
        stats = w.run_once()
    assert stats.done == 3 and len(p.calls) == 1
    for lemma in ("gardener", "cheap", "nobody"):
        assert any(f"这条属于 {lemma}" in t for t in hook_texts(env["db"], lemma))


def test_wrong_id_element_is_dropped_and_only_that_job_retries(env: dict, logs: list):
    """一条输出的 id 是模型编的：丢掉它、只重试它，同批其余照常落库。"""
    for lemma in ("gardener", "cheap"):
        enqueue(env["db"], lemma, priority=0)
    p = FakeProvider(wrong_id_on="gardener", hooks_for=marked_hooks)
    with make_worker(env, provider=p, batch_size=2, retries=1, logs=logs) as w:
        stats = w.run_once()

    assert stats.done == 1 and stats.failed == 1
    # 下一轮只重试没对上的那条，不重发已经成功的
    assert [[i["lemma"] for i in c] for c in p.calls] == [
        ["gardener", "cheap"], ["gardener"]
    ]
    assert any("对不上" in m for m in logs)
    assert any("这条属于 cheap" in t for t in hook_texts(env["db"], "cheap"))
    assert hook_texts(env["db"], "gardener") == []  # 串不进去，一行都没落


def test_missing_id_element_is_dropped(env: dict):
    """模型忘了带 id：整条作废（宁可重试，也不赌顺序）。"""
    enqueue(env["db"], "gardener")
    p = FakeProvider(drop_id_on="gardener")
    with make_worker(env, provider=p, batch_size=1, retries=1) as w:
        stats = w.run_once()
    assert stats.failed == 1 and stats.rows == 0 and stats.calls == 2


def test_duplicate_id_poisons_both_copies(env: dict):
    """同一个 id 回来两次：两条都不要，该任务重试。"""
    for lemma in ("gardener", "cheap"):
        enqueue(env["db"], lemma, priority=0)
    p = FakeProvider(duplicate_id_on="gardener", hooks_for=marked_hooks)
    with make_worker(env, provider=p, batch_size=2, retries=0) as w:
        stats = w.run_once()
    assert stats.done == 1 and stats.failed == 1
    assert hook_texts(env["db"], "gardener") == []
    assert any("这条属于 cheap" in t for t in hook_texts(env["db"], "cheap"))


def test_pack_id_is_the_job_id(env: dict):
    job = enqueue(env["db"], "gardener")
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
    assert p.calls[0][0]["id"] == str(job)


# --- Mnemonic 落库口径 -----------------------------------------------------


def test_rows_split_by_hook_type_plus_gloss_row(env: dict):
    enqueue(env["db"], "rain")  # mini 词典里有音标 → fake 会给 morph + pun
    with make_worker(env) as w:
        w.run_once()
    conn = get_conn(env["db"])
    lexeme_id = conn.execute("SELECT id FROM Lexeme WHERE lemma='rain'").fetchone()["id"]
    conn.close()
    rows = mnemonic_rows(env["db"], lexeme_id)
    assert [r["kind"] for r in rows] == ["gloss", "morph", "pun"]
    assert all(r["version"] == 1 and r["provider"] == "fake" for r in rows)
    assert json.loads(rows[0]["payload_json"])["text"].startswith("〔fake〕")


def test_duplicate_hook_types_get_suffixed_kinds(env: dict):
    """同一 type 两条 hook 不能撞 UNIQUE(lexeme_id, kind, version) 而丢内容。"""
    rows = A.AnnotateWorker.to_rows(
        {
            "context_gloss": "g",
            "hooks": [
                {"type": "pun", "text": "a", "label": "非词源"},
                {"type": "pun", "text": "b", "label": "非词源"},
            ],
        }
    )
    assert [k for k, _ in rows] == ["gloss", "pun", "pun#2"]


def test_version_increments_on_regeneration(env: dict):
    enqueue(env["db"], "gardener")
    with make_worker(env) as w:
        w.run_once()
    job2 = enqueue(env["db"], "gardener")
    with make_worker(env) as w:
        w.run_once()
    assert job_status(env["db"], job2)["status"] == "done"
    conn = get_conn(env["db"])
    lexeme_id = conn.execute(
        "SELECT id FROM Lexeme WHERE lemma='gardener'"
    ).fetchone()["id"]
    conn.close()
    versions = {r["version"] for r in mnemonic_rows(env["db"], lexeme_id)}
    assert versions == {1, 2}


def test_edited_by_user_is_never_overwritten(client: TestClient, env: dict, logs: list):
    """DESIGN §5：edited_by_user 置位后不被覆盖。"""
    enqueue(env["db"], "rain")
    with make_worker(env) as w:
        w.run_once()
    conn = get_conn(env["db"])
    lexeme_id = int(conn.execute("SELECT id FROM Lexeme WHERE lemma='rain'").fetchone()["id"])
    with conn:
        conn.execute(
            "UPDATE Mnemonic SET payload_json = ?, edited_by_user = 1 "
            "WHERE lexeme_id = ? AND kind = 'gloss'",
            (json.dumps({"text": "我自己写的释义"}, ensure_ascii=False), lexeme_id),
        )
    conn.close()

    enqueue(env["db"], "rain")  # 重新生成
    with make_worker(env, logs=logs) as w:
        stats = w.run_once()
    assert stats.done == 1
    assert any("已被用户编辑，跳过覆盖" in m for m in logs)

    rows = mnemonic_rows(env["db"], lexeme_id)
    gloss_rows = [r for r in rows if r["kind"] == "gloss"]
    assert len(gloss_rows) == 1  # 没有新版本盖上去
    assert json.loads(gloss_rows[0]["payload_json"])["text"] == "我自己写的释义"
    assert {r["version"] for r in rows if r["kind"] == "morph"} == {1, 2}

    # 端点侧看到的仍是用户那条
    body = client.get("/mnemonic", params={"lexeme_id": lexeme_id}).json()
    kinds = {m["kind"]: m for m in body["mnemonics"]}
    assert kinds["gloss"]["payload"]["text"] == "我自己写的释义"
    assert kinds["gloss"]["edited_by_user"] is True
    assert kinds["morph"]["version"] == 2


# --- 队列状态机 -------------------------------------------------------------


def test_reset_stale_running(env: dict):
    job = enqueue(env["db"], "gardener")
    conn = get_conn(env["db"])
    with conn:
        conn.execute("UPDATE AnnotationJob SET status='running' WHERE id = ?", (job,))
    conn.close()
    with make_worker(env) as w:
        assert w.reset_stale_running() == 1
        assert w.run_once().done == 1


def test_empty_queue_is_a_noop(env: dict):
    with make_worker(env) as w:
        stats = w.run_once()
    assert stats.as_dict()["picked"] == 0 and stats.calls == 0


def test_done_jobs_are_not_reprocessed(env: dict):
    enqueue(env["db"], "gardener")
    p = FakeProvider()
    with make_worker(env, provider=p) as w:
        w.run_once()
        w.run_once()
    assert len(p.calls) == 1


def test_limit_caps_jobs_per_run(env: dict):
    for lemma in ("gardener", "cheap", "nobody"):
        enqueue(env["db"], lemma)
    with make_worker(env, limit=2, batch_size=1) as w:
        assert w.run_once().done == 2


def test_run_loop_stops_after_max_rounds(env: dict):
    enqueue(env["db"], "gardener")
    with make_worker(env, sleep=lambda _s: None) as w:
        total = w.run_loop(poll=0, max_rounds=3)
    assert total.done == 1 and total.picked == 1


# --- dry-run / CLI ---------------------------------------------------------


def test_dry_run_writes_nothing(env: dict):
    enqueue(env["db"], "gardener")
    with make_worker(env, provider=FakeProvider(cost_per_item=0.5)) as w:
        info = w.dry_run()
    assert info["jobs"] == 1 and info["estimate_cny"] == 0.5
    assert info["packs"][0]["lemma"] == "gardener"
    conn = get_conn(env["db"])
    assert conn.execute("SELECT COUNT(*) c FROM Mnemonic").fetchone()["c"] == 0
    assert conn.execute(
        "SELECT status FROM AnnotationJob"
    ).fetchone()["status"] == "queued"
    conn.close()


def test_cli_dry_run_prints_estimate(env: dict, capsys):
    enqueue(env["db"], "gardener")
    rc = A.main(["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "dry-run" in out and "gardener" in out


def test_cli_dry_run_prints_deepseek_request_body(env: dict, capsys, monkeypatch):
    """dry-run 打印真会发出去的请求体：模型名 + thinking 关（工单 8a）。

    **零网络**：deepseek 没 key，payload() 也只是离线组装，不碰 _post()。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    enqueue(env["db"], "gardener")
    rc = A.main([
        "--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
        "--provider", "deepseek", "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0 and "请求体样例" in out
    body = json.loads(out.split("请求体样例(不发送): ")[1].splitlines()[0])
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert isinstance(body["messages"], str)      # prompt 正文折成摘要，不刷屏
    assert "authorization" not in json.dumps(body).lower()  # 样例里没有鉴权信息


def test_cli_model_flag_reaches_request_body(env: dict, capsys, monkeypatch):
    """--model 一路透传到请求体（CLI → build_provider → payload）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    enqueue(env["db"], "gardener")
    A.main([
        "--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
        "--provider", "deepseek", "--model", "deepseek-v4-flash-2512", "--dry-run",
    ])
    out = capsys.readouterr().out
    body = json.loads(out.split("请求体样例(不发送): ")[1].splitlines()[0])
    assert body["model"] == "deepseek-v4-flash-2512"
    assert body["thinking"] == {"type": "disabled"}


def test_cli_dry_run_sample_skipped_for_provider_without_payload(env: dict, capsys):
    """fake 没有 payload()：不打印样例，也不该报错。"""
    enqueue(env["db"], "gardener")
    rc = A.main(["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "请求体样例" not in out


# --- 工单 16-2：牌价 as_of 肉眼可见 + 覆盖值非法即 fail closed --------------


@pytest.fixture(autouse=True)
def _no_price_override(monkeypatch):
    """本机若设过牌价覆盖，估价类用例会莫名其妙地飘——先清干净。"""
    for name in (
        "POI_DEEPSEEK_PRICE_IN", "POI_DEEPSEEK_PRICE_OUT",
        "POI_ANTHROPIC_PRICE_IN", "POI_ANTHROPIC_PRICE_OUT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_dry_run_carries_price_as_of(env: dict, monkeypatch):
    """dry_run() 的返回里带牌价说明（含 as_of），别让估价数字裸奔。"""
    from app.providers import get_provider

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    enqueue(env["db"], "gardener")
    with make_worker(env, provider=get_provider("deepseek")) as w:
        info = w.dry_run()
    assert info["jobs"] == 1 and info["estimate_cny"] > 0
    assert "as_of=2026-08-16" in info["price"]
    assert info["price_as_of"] == "牌价 as_of=2026-08-16"


def test_dry_run_price_empty_for_provider_without_prices(env: dict):
    """fake 没有牌价概念：不打印、也不报错。"""
    enqueue(env["db"], "gardener")
    with make_worker(env, provider=FakeProvider(cost_per_item=0.5)) as w:
        info = w.dry_run()
    assert info["price"] == "" and info["price_as_of"] == ""


def test_cli_prints_price_as_of(env: dict, capsys, monkeypatch):
    """--dry-run 里"这是某天的牌价"肉眼可见（零网络：deepseek 无 key 也不发包）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    enqueue(env["db"], "gardener")
    rc = A.main([
        "--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
        "--provider", "deepseek", "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "牌价 as_of=2026-08-16" in out          # 启动行的短标注
    assert "牌价(估算依据):" in out and "official price page" in out
    assert "以官方现价为准" in out


def test_cli_run_summary_omits_price_for_fake(env: dict, capsys):
    """fake 没牌价：收尾行不带 as_of，不刷无意义的字。"""
    enqueue(env["db"], "gardener")
    rc = A.main(["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--once"])
    out = capsys.readouterr().out
    assert rc == 0 and "est_cost=" in out and "牌价 as_of" not in out


def test_illegal_price_env_override_stops_the_run(env: dict, capsys, monkeypatch):
    """POI_DEEPSEEK_PRICE_IN=-1 → 估价失败 → 本轮停手、一次调用不发、退出码 3。

    真 provider（deepseek）在这条路径上根本走不到 HTTP：估价先炸。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", "-1")
    job = enqueue(env["db"], "gardener", priority=0)
    rc = A.main([
        "--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
        "--provider", "deepseek", "--once",
    ])
    out = capsys.readouterr().out
    assert rc == A.EXIT_ESTIMATE_BROKEN == 3
    assert "估价不可用" in out and "skipped_estimate=1" in out
    assert "POI_DEEPSEEK_PRICE_IN" in out  # 说清楚是哪个环境变量配坏了
    assert job_status(env["db"], job)["status"] == "queued"  # 任务原地留着


@pytest.mark.parametrize("bad", ["abc", "nan", "-0.5"])
def test_illegal_price_env_override_blocks_high_priority_too(
    env: dict, monkeypatch, bad
):
    """收藏（高优先级）同样停：不确定成本时一分钱都不许花。"""
    from app.providers import get_provider

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", bad)
    enqueue(env["db"], "gardener", priority=A.HIGH_PRIORITY)
    with make_worker(env, provider=get_provider("deepseek")) as w:
        stats = w.run_once()
    assert stats.estimate_broken and stats.skipped_estimate == 1
    assert stats.calls == 0 and stats.est_cost == 0.0


def test_valid_price_env_override_is_honoured_end_to_end(env: dict, monkeypatch):
    """合法覆盖值真的进了预算账：把牌价抬高 100 倍，预算立刻被吃满。"""
    from app.providers import get_provider

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", "300")
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", "900")
    job = enqueue(env["db"], "gardener", priority=0)
    with make_worker(env, provider=get_provider("deepseek"), budget=0.05) as w:
        stats = w.run_once()
    assert stats.calls == 0 and stats.skipped_budget == 1  # 预算截断，不是估价坏
    assert not stats.estimate_broken
    assert job_status(env["db"], job)["status"] == "queued"


def test_cli_unknown_provider_returns_nonzero(env: dict, capsys):
    rc = A.main(["--db", str(env["db"]), "--provider", "gpt-9", "--once"])
    assert rc == 2 and "provider 初始化失败" in capsys.readouterr().out


def test_cli_summary_line(env: dict, capsys):
    enqueue(env["db"], "gardener")
    rc = A.main(["--db", str(env["db"]), "--ecdict", str(env["ecdict"]), "--once"])
    out = capsys.readouterr().out
    assert rc == 0 and "done=1" in out and "failed=0" in out


def test_build_provider_disables_inner_retries_for_real_providers(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = A.build_provider("deepseek")
    assert p.retries == 0  # 重试归 worker 管，不叠加烧钱
    assert A.build_provider("fake").name == "fake"


def test_build_provider_passes_model_through(monkeypatch):
    """--model 透传（工单 8a）。构造不发网络、不校验 key。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert A.build_provider("deepseek").model == "deepseek-v4-flash"
    assert A.build_provider("deepseek", "deepseek-v4").model == "deepseek-v4"
    # fake 不认 model 参数：只能忽略，不许炸
    assert A.build_provider("fake", "deepseek-v4").name == "fake"


def test_cli_model_flag_reaches_build_provider(env: dict, monkeypatch, capsys):
    """--model 从命令行一路走到 build_provider（跑的仍然是 fake，零真实调用）。"""
    seen: dict = {}
    real = A.build_provider

    def spy(name, model=None):
        seen["name"], seen["model"] = name, model
        return real("fake")

    monkeypatch.setattr(A, "build_provider", spy)
    enqueue(env["db"], "gardener")
    rc = A.main([
        "--db", str(env["db"]), "--ecdict", str(env["ecdict"]),
        "--provider", "deepseek", "--model", "deepseek-v4-flash-2512", "--once",
    ])
    assert rc == 0
    assert seen == {"name": "deepseek", "model": "deepseek-v4-flash-2512"}


# --- 分层（工单 9：worker 不许反向依赖 web 层） -----------------------------


def test_worker_does_not_import_web_layer():
    """`import app.annotate` 不许把 fastapi/starlette/pydantic/uvicorn 拖进来。

    ECDICT 口径抽到 app/ecdict.py 之前，worker `from app.server import ...`，
    一个后台任务要靠整条 web 栈才能起来（DESIGN §7 重构债）。这里在**干净的
    子进程**里断言 sys.modules，本进程早就 import 过 fastapi，断言不了。
    """
    import subprocess

    code = (
        "import sys; import app.annotate;"
        "web = sorted(m for m in sys.modules"
        " if m.split('.')[0] in ('fastapi','starlette','uvicorn','pydantic'));"
        "print(web)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", f"worker 又把 web 层拖进来了：{proc.stdout}"


def test_worker_and_server_share_one_ecdict_implementation():
    """口径只有一份：两边用的必须是 app.ecdict 里的同一个对象。"""
    from app import ecdict as E
    from app import server as S

    assert A.EcdictStore is E.EcdictStore is S.EcdictStore
    assert A.fill_from_ecdict is E.fill_from_ecdict is S.fill_from_ecdict
    assert A.HIGH_PRIORITY == S.COLLECT_JOB_PRIORITY
