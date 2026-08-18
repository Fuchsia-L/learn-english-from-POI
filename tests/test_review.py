"""M1 复习闭环：队列纳入/排除、排序、毕业规则、幂等答题、UTC 存储、三个端点。

夹具一律自造（conftest 的 FIXTURE_SRT + build_ecdict 的 mini 词典）。
规则层（app/review.py）用注入的 `now` 做时间旅行，不 sleep、不 patch 系统时钟；
端点层（app/server.py）走 TestClient，用真实 now。全文件零网络请求。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app import review as R  # noqa: E402
from app.db import init_db  # noqa: E402
from app.ingest import ingest_srt  # noqa: E402
from app.server import create_app  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    srt = tmp_path / "fixture.srt"
    srt.write_text(FIXTURE_SRT, encoding="utf-8")
    ecdict = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)
    db = tmp_path / "poi.db"
    stats = ingest_srt(db_path=db, srt_path=srt, title="Test Show", season_ep="s01e01")
    return {"db": db, "ecdict": ecdict, "content_id": stats["content_id"]}


@pytest.fixture()
def conn(env: dict):
    c = init_db(env["db"])
    yield c
    c.close()


@pytest.fixture()
def client(env: dict):
    app = create_app(db_path=env["db"], ecdict_path=env["ecdict"])
    with TestClient(app) as c:
        c.env = env  # type: ignore[attr-defined]
        yield c


def days(n: float) -> timedelta:
    return timedelta(days=n)


def add_entry(conn: sqlite3.Connection, lemma: str, added: datetime) -> int:
    """建一个收藏条目（含 Lexeme），added_at 可以是过去的时间。"""
    row = conn.execute("SELECT id FROM Lexeme WHERE lemma = ?", (lemma,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO Lexeme (lemma, ipa, dict_gloss) VALUES (?,?,?)",
            (lemma, f"/{lemma}/", f"{lemma} 的释义"),
        )
        lexeme_id = int(cur.lastrowid)
    else:
        lexeme_id = int(row["id"])
    cur = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?,?)",
        (lexeme_id, R.iso(added)),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_review(conn: sqlite3.Connection, entry_id: int, result: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO Review (vocab_entry_id, at, result) VALUES (?,?,?)",
        (entry_id, R.iso(at), result),
    )
    conn.commit()


def due_ids(conn: sqlite3.Connection, now: datetime = NOW) -> list[int]:
    return [s.id for s in R.due_states(conn, now)]


# --- 队列纳入 / 排除 --------------------------------------------------------


def test_recent_entry_is_due(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    assert due_ids(conn) == [e]


def test_entry_added_today_is_due(conn):
    e = add_entry(conn, "stakeout", NOW - days(0.01))
    assert due_ids(conn) == [e]


def test_old_entry_falls_out_of_window(conn):
    add_entry(conn, "stakeout", NOW - days(R.REVIEW_WINDOW_DAYS + 1))
    assert due_ids(conn) == []


def test_window_boundary_is_inclusive(conn):
    inside = add_entry(conn, "cop", NOW - days(R.REVIEW_WINDOW_DAYS) + timedelta(minutes=1))
    add_entry(conn, "foodie", NOW - days(R.REVIEW_WINDOW_DAYS) - timedelta(minutes=1))
    assert due_ids(conn) == [inside]


def test_old_entry_with_dont_stays_in_queue(conn):
    """出过错的词一直跟着你，哪怕早就滚出 7 天窗口。"""
    e = add_entry(conn, "stakeout", NOW - days(30))
    add_review(conn, e, R.RESULT_DONT, NOW - days(20))
    assert due_ids(conn) == [e]


def test_graduated_entry_leaves_queue(conn):
    e = add_entry(conn, "stakeout", NOW - days(10))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(5))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(4))
    states = {s.id: s for s in R.entry_states(conn, NOW)}
    assert states[e].graduated is True
    assert due_ids(conn) == []


def test_reviewed_today_leaves_queue_until_tomorrow(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    add_review(conn, e, R.RESULT_KNOW, NOW - timedelta(hours=2))
    assert due_ids(conn) == []  # 今天已经过一遍了
    assert due_ids(conn, NOW + days(1)) == [e]  # 明天再来


# --- 排序 ------------------------------------------------------------------


def test_never_reviewed_first_then_oldest_review(conn):
    fresh = add_entry(conn, "stakeout", NOW - days(1))  # 从未复习
    old_review = add_entry(conn, "cop", NOW - days(6))
    new_review = add_entry(conn, "foodie", NOW - days(6))
    add_review(conn, old_review, R.RESULT_DONT, NOW - days(5))
    add_review(conn, new_review, R.RESULT_DONT, NOW - days(2))
    assert due_ids(conn) == [fresh, old_review, new_review]


def test_ties_break_by_added_then_id(conn):
    first = add_entry(conn, "cop", NOW - days(3))
    second = add_entry(conn, "foodie", NOW - days(2))
    assert due_ids(conn) == [first, second]


# --- 毕业规则 --------------------------------------------------------------


def test_two_knows_too_soon_after_collect_do_not_graduate(conn):
    """连续 2 次 know 但距首次收藏不足 3 天 → 不毕业（当天收藏当天狂点没用）。"""
    e = add_entry(conn, "stakeout", NOW - days(2))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(1.5))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(0.5))
    st = R.entry_states(conn, NOW)[0]
    assert st.know_streak == 2 and st.graduated is False
    assert due_ids(conn) == []  # 今天已复习，明天还回来
    assert due_ids(conn, NOW + days(1)) == [e]


def test_streak_completed_later_graduates(conn):
    """满 3 天之后再答对一次就毕业（streak 已够，只差年龄）。"""
    e = add_entry(conn, "stakeout", NOW - days(4))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(3.5))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(2.5))  # 距收藏 1.5 天，不够
    assert R.entry_states(conn, NOW)[0].graduated is False
    add_review(conn, e, R.RESULT_KNOW, NOW - days(0.5))  # 距收藏 3.5 天，够了
    assert R.entry_states(conn, NOW)[0].graduated is True


def test_dont_resets_streak(conn):
    e = add_entry(conn, "stakeout", NOW - days(10))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(6))
    add_review(conn, e, R.RESULT_DONT, NOW - days(5))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(4))
    st = R.entry_states(conn, NOW)[0]
    assert st.know_streak == 1 and st.graduated is False and st.due is True


def test_single_know_is_not_enough(conn):
    e = add_entry(conn, "stakeout", NOW - days(10))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(4))
    assert R.entry_states(conn, NOW)[0].graduated is False


# --- 答题（幂等 / 校验 / UTC） ---------------------------------------------


def test_answer_writes_one_row(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    out = R.answer(conn, e, "know", NOW)
    assert out["recorded"] is True and out["duplicate"] is False
    assert out["know_streak"] == 1 and out["graduated"] is False
    n = conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"]
    assert n == 1


def test_answer_same_day_same_result_is_idempotent(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    first = R.answer(conn, e, "know", NOW)
    again = R.answer(conn, e, "know", NOW + timedelta(hours=3))
    assert again["duplicate"] is True and again["recorded"] is False
    assert again["at"] == first["at"]  # 时间戳仍是第一次那条
    assert conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"] == 1
    assert again["know_streak"] == 1


def test_answer_changed_mind_same_day_is_recorded(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    R.answer(conn, e, "know", NOW)
    out = R.answer(conn, e, "dont", NOW + timedelta(hours=1))
    assert out["recorded"] is True and out["know_streak"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"] == 2


def test_answer_next_day_appends(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    R.answer(conn, e, "know", NOW)
    out = R.answer(conn, e, "know", NOW + days(1))
    assert out["duplicate"] is False and out["know_streak"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"] == 2


def test_answer_rejects_bad_result(conn):
    e = add_entry(conn, "stakeout", NOW)
    with pytest.raises(R.BadResult):
        R.answer(conn, e, "maybe", NOW)
    with pytest.raises(R.BadResult):
        R.answer(conn, e, "", NOW)


def test_answer_accepts_case_and_spaces(conn):
    e = add_entry(conn, "stakeout", NOW)
    assert R.answer(conn, e, " KNOW ", NOW)["result"] == "know"


def test_answer_unknown_entry(conn):
    with pytest.raises(R.UnknownEntry):
        R.answer(conn, 4242, "know", NOW)


def test_review_at_is_stored_in_utc(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    # 用一个带非 UTC 时区的 now：库里必须是 UTC 时刻，不是本地墙上时间
    local = datetime(2026, 8, 18, 23, 30, tzinfo=timezone(timedelta(hours=8)))
    R.answer(conn, e, "know", local)
    raw = conn.execute("SELECT at FROM Review").fetchone()["at"]
    assert raw.endswith("+00:00")
    parsed = R.parse_ts(raw)
    assert parsed == local.astimezone(timezone.utc)
    assert parsed.hour == 15 and parsed.date().isoformat() == "2026-08-18"


def test_parse_ts_accepts_naive_and_z(conn):
    assert R.parse_ts("2026-08-18T04:00:00Z") == datetime(
        2026, 8, 18, 4, tzinfo=timezone.utc
    )
    assert R.parse_ts("2026-08-18T04:00:00") == datetime(
        2026, 8, 18, 4, tzinfo=timezone.utc
    )
    assert R.parse_ts("") is None and R.parse_ts("不是时间") is None


def test_naive_added_at_is_read_as_utc(conn):
    """老行（没带时区）按 UTC 解释，不按本机时区——否则窗口会漂。"""
    lex = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
    conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?,?)",
        (int(lex.lastrowid), "2026-08-17T12:00:00"),
    )
    conn.commit()
    assert len(due_ids(conn)) == 1


# --- 统计 ------------------------------------------------------------------


def test_stats_counts(conn):
    due_e = add_entry(conn, "stakeout", NOW - days(1))
    done_e = add_entry(conn, "cop", NOW - days(2))
    grad_e = add_entry(conn, "foodie", NOW - days(9))
    add_review(conn, done_e, R.RESULT_DONT, NOW - timedelta(hours=1))  # 今天复习过
    add_review(conn, grad_e, R.RESULT_KNOW, NOW - days(5))
    add_review(conn, grad_e, R.RESULT_KNOW, NOW - days(4))
    s = R.stats(conn, NOW)
    assert s["date"] == "2026-08-18"
    assert s["reviewed_today"] == 1 and s["dont_today"] == 1 and s["know_today"] == 0
    assert s["due"] == 1 and s["graduated"] == 1 and s["total"] == 3
    assert due_ids(conn) == [due_e]
    assert s["rules"]["window_days"] == R.REVIEW_WINDOW_DAYS


def test_stats_empty_db(conn):
    s = R.stats(conn, NOW)
    assert s["due"] == 0 and s["total"] == 0 and s["graduated"] == 0


# --- 卡片装配 --------------------------------------------------------------


def test_next_cards_carry_gloss_ipa_and_sentence(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    seg = next(s for s in segs if "cousins" in s["text_en"])
    assert client.post(
        "/collect", json={"surface": "cousins", "segment_id": seg["id"]}
    ).status_code == 200

    body = client.get("/review/next").json()
    assert body["count"] == 1 and body["remaining"] == 1
    card = body["cards"][0]
    assert card["lemma"] == "cousin"
    assert card["dict_gloss"] and card["ipa"]  # mini 词典回填过了
    assert card["encounter"]["sentence"] == seg["text_en"]
    assert card["encounter"]["segment_id"] == seg["id"]
    assert card["encounter"]["season_ep"] == "s01e01"
    # 收藏会建高优先级 AnnotationJob，助记还没生成 → queued
    assert card["mnemonic_status"] == "queued" and card["has_mnemonic"] is False
    assert card["know_streak"] == 0 and card["last_reviewed_at"] is None


def test_card_reports_mnemonic_ready(client: TestClient, env: dict):
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    r = client.post("/collect", json={"surface": "home", "segment_id": segs[0]["id"]})
    lexeme_id = r.json()["lexeme_id"]
    conn = init_db(env["db"])
    conn.execute(
        "INSERT INTO Mnemonic (lexeme_id, kind, payload_json, provider, version)"
        " VALUES (?,?,?,?,1)",
        (lexeme_id, "gloss", '{"text": "自造助记"}', "fake"),
    )
    conn.commit()
    conn.close()
    card = client.get("/review/next").json()["cards"][0]
    assert card["has_mnemonic"] is True and card["mnemonic_status"] == "done"


def test_card_without_encounter(conn):
    """预热词（没有 encounter）也能进队，encounter 为 null 而不是崩。"""
    e = add_entry(conn, "stakeout", NOW - days(1))
    card = R.cards(conn, R.due_states(conn, NOW))[0]
    assert card["vocab_entry_id"] == e and card["encounter"] is None
    assert card["mnemonic_status"] == "none"


# --- 端点 ------------------------------------------------------------------


def collect_some(client: TestClient, env: dict, surfaces: list[str]) -> list[int]:
    segs = client.get("/segments", params={"content_id": env["content_id"]}).json()[
        "segments"
    ]
    ids = []
    for surface in surfaces:
        seg = next(s for s in segs if surface in s["text_en"].lower())
        r = client.post("/collect", json={"surface": surface, "segment_id": seg["id"]})
        assert r.status_code == 200
        ids.append(r.json()["vocab_entry_id"])
    return ids


def test_review_next_empty(client: TestClient):
    body = client.get("/review/next").json()
    assert body == {
        "count": 0,
        "remaining": 0,
        "limit": R.DEFAULT_LIMIT,
        "cards": [],
        "rules": R.rules(),
    }


def test_review_next_limit_does_not_change_remaining(client: TestClient, env: dict):
    collect_some(client, env, ["cousins", "cameras", "gardener"])
    body = client.get("/review/next", params={"limit": 2}).json()
    assert body["count"] == 2 and body["remaining"] == 3 and body["limit"] == 2


def test_review_next_rejects_bad_limit(client: TestClient):
    assert client.get("/review/next", params={"limit": 0}).status_code == 422
    assert client.get("/review/next", params={"limit": 9999}).status_code == 422


def test_review_answer_endpoint_roundtrip(client: TestClient, env: dict):
    (entry_id,) = collect_some(client, env, ["cousins"])
    r = client.post(
        "/review/answer", json={"vocab_entry_id": entry_id, "result": "know"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["recorded"] is True and body["know_streak"] == 1
    assert body["remaining"] == 0  # 今天这张卡过完了
    assert body["at"].endswith("+00:00")
    # 幂等：再答一次同样的答案不再插行
    again = client.post(
        "/review/answer", json={"vocab_entry_id": entry_id, "result": "know"}
    ).json()
    assert again["duplicate"] is True and again["reviews"] == 1
    assert client.get("/review/next").json()["remaining"] == 0


def test_review_answer_dont_keeps_card_for_tomorrow(client: TestClient, env: dict):
    (entry_id,) = collect_some(client, env, ["cousins"])
    body = client.post(
        "/review/answer", json={"vocab_entry_id": entry_id, "result": "dont"}
    ).json()
    assert body["know_streak"] == 0 and body["graduated"] is False
    conn = init_db(env["db"])
    try:
        tomorrow = R.now_utc() + days(1)
        assert [s.id for s in R.due_states(conn, tomorrow)] == [entry_id]
    finally:
        conn.close()


def test_review_answer_404_and_400(client: TestClient, env: dict):
    (entry_id,) = collect_some(client, env, ["cousins"])
    assert client.post(
        "/review/answer", json={"vocab_entry_id": 99999, "result": "know"}
    ).status_code == 404
    bad = client.post(
        "/review/answer", json={"vocab_entry_id": entry_id, "result": "maybe"}
    )
    assert bad.status_code == 400 and "know" in bad.json()["detail"]
    assert client.post("/review/answer", json={"result": "know"}).status_code == 422


def test_review_stats_endpoint(client: TestClient, env: dict):
    ids = collect_some(client, env, ["cousins", "cameras"])
    client.post("/review/answer", json={"vocab_entry_id": ids[0], "result": "know"})
    s = client.get("/review/stats").json()
    assert s["total"] == 2 and s["reviewed_today"] == 1 and s["know_today"] == 1
    assert s["due"] == 1 and s["graduated"] == 0
    assert s["rules"]["graduate_streak"] == R.GRADUATE_STREAK
    assert s["date"] == R.now_utc().date().isoformat()


def test_review_endpoints_make_no_network_calls(client: TestClient, env: dict, monkeypatch):
    import socket

    (entry_id,) = collect_some(client, env, ["cousins"])

    class Boom(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError("复习端点不应发起任何网络调用")

    monkeypatch.setattr(socket, "socket", Boom)
    assert client.get("/review/next").status_code == 200
    assert client.post(
        "/review/answer", json={"vocab_entry_id": entry_id, "result": "dont"}
    ).status_code == 200
    assert client.get("/review/stats").status_code == 200
