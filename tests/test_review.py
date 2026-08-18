"""M1 复习闭环：stage 推导、间隔调度、队列纳入/排除、排序、幂等答题、UTC 存储、三个端点。

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


def state_of(conn: sqlite3.Connection, entry_id: int, now: datetime = NOW) -> R.EntryState:
    return next(s for s in R.entry_states(conn, now) if s.id == entry_id)


def day_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).date().isoformat()


# --- 规则常量本身 ----------------------------------------------------------


def test_rules_are_the_simplified_sr_ladder(conn):
    """口径体检：3 次封顶、间隔表与毕业档对齐（改常量时这条先响）。"""
    assert R.GRADUATE_STAGE == 3 == len(R.INTERVALS)
    assert list(R.INTERVALS) == [1, 3, 7]
    assert R.interval_days(0) == R.DONT_INTERVAL_DAYS == 1
    assert [R.interval_days(s) for s in (1, 2, 3)] == [1, 3, 7]
    assert R.rules() == {
        "intervals": [1, 3, 7],
        "graduate_stage": 3,
        "dont_interval_days": 1,
        "once_per_day": True,
        "results": ["know", "dont"],
    }


# --- 队列纳入 / 排除 --------------------------------------------------------


def test_new_entry_is_due_the_day_it_was_collected(conn):
    e = add_entry(conn, "stakeout", NOW - days(0.01))
    st = state_of(conn, e)
    assert st.stage == 0 and st.next_due == day_str(NOW) and st.due is True
    assert due_ids(conn) == [e]


def test_never_reviewed_old_entry_stays_due(conn):
    """没复习过的老词不再"滚出窗口沉底"：它只是逾期越来越久。"""
    e = add_entry(conn, "stakeout", NOW - days(30))
    st = state_of(conn, e)
    assert st.due is True and st.overdue_days == 30
    assert due_ids(conn) == [e]


def test_entry_not_due_yet_is_out_of_queue(conn):
    e = add_entry(conn, "stakeout", NOW - days(3))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(1))  # 昨天答对 → stage 1，间隔 1 天
    st = state_of(conn, e)
    assert st.stage == 1 and st.next_due == day_str(NOW)  # 今天正好到期
    assert due_ids(conn) == [e]
    # 再来一次（今天答对）→ stage 2，3 天后才回来
    add_review(conn, e, R.RESULT_KNOW, NOW)
    assert due_ids(conn, NOW + days(1)) == []
    assert due_ids(conn, NOW + days(2)) == []
    assert due_ids(conn, NOW + days(3)) == [e]


def test_graduated_entry_leaves_queue_forever(conn):
    e = add_entry(conn, "stakeout", NOW - days(10))
    for d in (5, 4, 1):
        add_review(conn, e, R.RESULT_KNOW, NOW - days(d))
    st = state_of(conn, e)
    assert st.stage == R.GRADUATE_STAGE and st.graduated is True
    assert due_ids(conn) == []
    assert due_ids(conn, NOW + days(365)) == []  # 3 次封顶，之后不再打扰


def test_reviewed_today_leaves_queue_until_tomorrow(conn):
    """once_per_day：今天答错的词也得等到明天（不会当天原地复读）。"""
    e = add_entry(conn, "stakeout", NOW - days(1))
    add_review(conn, e, R.RESULT_DONT, NOW - timedelta(hours=2))
    assert due_ids(conn) == []
    assert due_ids(conn, NOW + days(1)) == [e]


# --- 间隔推进 --------------------------------------------------------------


def test_know_advances_stage_by_the_interval_table(conn):
    e = add_entry(conn, "stakeout", NOW)

    R.answer(conn, e, "know", NOW)  # stage 1 → +1 天
    st = state_of(conn, e)
    assert st.stage == 1 and st.next_due == day_str(NOW + days(1))
    assert st.due is False and due_ids(conn, NOW + days(1)) == [e]

    R.answer(conn, e, "know", NOW + days(1))  # stage 2 → +3 天
    st = state_of(conn, e, NOW + days(1))
    assert st.stage == 2 and st.next_due == day_str(NOW + days(4))
    assert st.graduated is False
    assert due_ids(conn, NOW + days(3)) == [] and due_ids(conn, NOW + days(4)) == [e]

    R.answer(conn, e, "know", NOW + days(4))  # stage 3 = 毕业
    st = state_of(conn, e, NOW + days(4))
    assert st.stage == 3 and st.graduated is True and st.due is False


def test_dont_resets_stage_to_zero_and_returns_tomorrow(conn):
    e = add_entry(conn, "stakeout", NOW - days(10))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(9))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(8))  # stage 2
    assert state_of(conn, e).stage == 2

    R.answer(conn, e, "dont", NOW)
    st = state_of(conn, e)
    assert st.stage == 0 and st.graduated is False
    assert st.next_due == day_str(NOW + days(1))
    assert due_ids(conn) == []  # 今天不复读
    assert due_ids(conn, NOW + days(1)) == [e]  # 明天从头再来


def test_dont_after_graduation_would_not_resurrect(conn):
    """毕业靠 stage 判定：毕业之后不再进队，也就不会再有 dont 把它拽回来。"""
    e = add_entry(conn, "stakeout", NOW - days(10))
    for d in (9, 8, 7):
        add_review(conn, e, R.RESULT_KNOW, NOW - days(d))
    assert state_of(conn, e).graduated is True
    # 万一有人手工塞了一条 dont（或换了规则重推导）：stage 归零，词回到队列
    add_review(conn, e, R.RESULT_DONT, NOW - days(2))
    st = state_of(conn, e)
    assert st.stage == 0 and st.graduated is False and st.due is True


def test_stage_is_capped_by_graduate_stage(conn):
    e = add_entry(conn, "stakeout", NOW - days(20))
    for d in range(19, 10, -1):
        add_review(conn, e, R.RESULT_KNOW, NOW - days(d))
    assert state_of(conn, e).stage == R.GRADUATE_STAGE


# --- 排序 ------------------------------------------------------------------


def test_most_overdue_first(conn):
    """逾期最久的排最前（next_due 最早）。"""
    just_due = add_entry(conn, "cop", NOW - days(1))
    add_review(conn, just_due, R.RESULT_DONT, NOW - days(1))  # next_due = 今天
    late = add_entry(conn, "foodie", NOW - days(9))
    add_review(conn, late, R.RESULT_DONT, NOW - days(8))  # next_due = 7 天前
    mid = add_entry(conn, "gardener", NOW - days(4))
    add_review(conn, mid, R.RESULT_DONT, NOW - days(3))  # next_due = 2 天前
    assert due_ids(conn) == [late, mid, just_due]


def test_never_reviewed_beats_reviewed_on_the_same_due_day(conn):
    reviewed = add_entry(conn, "cop", NOW - days(6))
    add_review(conn, reviewed, R.RESULT_DONT, NOW - days(3))  # next_due = 2 天前
    fresh = add_entry(conn, "foodie", NOW - days(2))  # next_due = 2 天前（收藏日）
    assert [s.next_due for s in R.due_states(conn, NOW)] == [
        day_str(NOW - days(2)),
        day_str(NOW - days(2)),
    ]
    assert due_ids(conn) == [fresh, reviewed]


def test_ties_break_by_added_then_id(conn):
    first = add_entry(conn, "cop", NOW - days(3))
    second = add_entry(conn, "foodie", NOW - days(3) + timedelta(minutes=5))
    assert due_ids(conn) == [first, second]


# --- 旧库兼容（纯读侧重推导，无迁移） --------------------------------------


def test_legacy_reviews_are_replayed_under_the_new_rules(conn):
    """老库里按"连对 2 次 + 满 3 天"毕业的词，现在只算 stage 2，还得再答一次。"""
    e = add_entry(conn, "stakeout", NOW - days(17))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(12))
    add_review(conn, e, R.RESULT_KNOW, NOW - days(11))
    st = state_of(conn, e)
    assert st.stage == 2 and st.graduated is False
    assert st.next_due == day_str(NOW - days(8))  # 11 天前 + 3 天
    assert st.due is True and st.overdue_days == 8


def test_legacy_naive_timestamps_derive_stage_and_due(conn):
    """老行没带时区：按 UTC 解释，stage / next_due 照样算得出来。"""
    lex = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
    entry = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?,?)",
        (int(lex.lastrowid), "2026-08-10T12:00:00"),
    )
    conn.execute(
        "INSERT INTO Review (vocab_entry_id, at, result) VALUES (?,?,?)",
        (int(entry.lastrowid), "2026-08-16T12:00:00", "know"),
    )
    conn.commit()
    st = R.entry_states(conn, NOW)[0]
    assert st.stage == 1 and st.next_due == "2026-08-17" and st.due is True


# --- 答题（幂等 / 校验 / UTC） ---------------------------------------------


def test_answer_writes_one_row(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    out = R.answer(conn, e, "know", NOW)
    assert out["recorded"] is True and out["duplicate"] is False
    assert out["know_streak"] == 1 and out["graduated"] is False
    assert out["stage"] == 1 and out["next_due"] == day_str(NOW + days(1))
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
    assert again["stage"] == 1  # 幂等：不会因为手抖被推到 stage 2


def test_answer_changed_mind_same_day_is_recorded(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    R.answer(conn, e, "know", NOW)
    out = R.answer(conn, e, "dont", NOW + timedelta(hours=1))
    assert out["recorded"] is True and out["know_streak"] == 0
    # 改口按事件流最新一行算：stage 回到 0，明天再来
    assert out["stage"] == 0 and out["next_due"] == day_str(NOW + days(1))
    assert conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"] == 2


def test_answer_next_day_appends(conn):
    e = add_entry(conn, "stakeout", NOW - days(1))
    R.answer(conn, e, "know", NOW)
    out = R.answer(conn, e, "know", NOW + days(1))
    assert out["duplicate"] is False and out["know_streak"] == 2
    assert out["stage"] == 2 and out["next_due"] == day_str(NOW + days(4))
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
    """老行（没带时区）按 UTC 解释，不按本机时区——否则到期日会漂。"""
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
    for d in (5, 4, 2):
        add_review(conn, grad_e, R.RESULT_KNOW, NOW - days(d))  # 三次答对 → 毕业
    s = R.stats(conn, NOW)
    assert s["date"] == "2026-08-18"
    assert s["reviewed_today"] == 1 and s["dont_today"] == 1 and s["know_today"] == 0
    assert s["due"] == 1 and s["graduated"] == 1 and s["total"] == 3
    assert due_ids(conn) == [due_e]
    assert s["rules"]["intervals"] == list(R.INTERVALS)


def test_stats_reports_stage_distribution(conn):
    fresh = add_entry(conn, "stakeout", NOW - days(1))  # stage 0
    one = add_entry(conn, "cop", NOW - days(4))
    add_review(conn, one, R.RESULT_KNOW, NOW - days(3))  # stage 1
    two = add_entry(conn, "foodie", NOW - days(6))
    add_review(conn, two, R.RESULT_KNOW, NOW - days(5))
    add_review(conn, two, R.RESULT_KNOW, NOW - days(4))  # stage 2
    grad = add_entry(conn, "gardener", NOW - days(9))
    for d in (8, 7, 6):
        add_review(conn, grad, R.RESULT_KNOW, NOW - days(d))  # stage 3 = 毕业
    s = R.stats(conn, NOW)
    assert s["stages"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    assert sum(s["stages"].values()) == s["total"] == 4
    assert s["stages"][str(R.GRADUATE_STAGE)] == s["graduated"]
    assert {fresh, one, two} >= set(due_ids(conn))


def test_stats_empty_db(conn):
    s = R.stats(conn, NOW)
    assert s["due"] == 0 and s["total"] == 0 and s["graduated"] == 0
    assert s["stages"] == {"0": 0, "1": 0, "2": 0, "3": 0}


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
    # 新收藏：stage 0、当天到期（前端拿它画"逾期/新词"标记）
    assert card["stage"] == 0 and card["overdue_days"] == 0
    assert card["next_due"] == R.now_utc().date().isoformat()


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
    assert body["stage"] == 1 and body["graduated"] is False
    tomorrow = (R.now_utc() + days(1)).date().isoformat()
    assert body["next_due"] == tomorrow  # 答对 → 1 天后再问
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
    assert body["stage"] == 0
    assert body["next_due"] == (R.now_utc() + days(1)).date().isoformat()
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
    assert s["stages"] == {"0": 1, "1": 1, "2": 0, "3": 0}
    assert s["rules"]["graduate_stage"] == R.GRADUATE_STAGE
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
