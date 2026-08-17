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
            raise RuntimeError("估价也爆炸")

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
