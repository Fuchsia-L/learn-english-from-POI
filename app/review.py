"""M1 复习最小闭环的规则与查询（DESIGN §7「最近几天滚动 + 会/不会」）。

**不做 FSRS**，不碰 LLM，不 import fastapi：纯 SQLite + 标准库，
HTTP 壳子在 app/server.py（`/review/next`、`/review/answer`、`/review/stats`）。

口径（全部写成本模块顶部的常量，改规则只改常量）:

1. 进队（due）—— 三个条件全满足：
   a. 还没毕业；
   b. 今天（UTC 日）还没复习过（ONCE_PER_DAY）；
   c. `added_at` 在最近 REVIEW_WINDOW_DAYS 天内 **或** 历史上答过 "dont"。
      —— 出过错的词一直跟着你，直到毕业为止；没出过错的词滚出 7 天窗口就自然沉底。
2. 排序：从未复习过的最优先，其次「上次复习时间最早」优先；
   同档按 added_at 早、id 小排（稳定，不随机）。
3. 毕业：末尾**连续** GRADUATE_STREAK 次 "know"，且最后一次复习距首次收藏
   ≥ GRADUATE_MIN_AGE_DAYS 天。毕业后不再进队（stats 里单独计数）。
   —— 用「最后一次复习时间」而非 now 判年龄：当天收藏、当天连点两次 know
   不算毕业，必须真的隔几天还认得。
4. 答题幂等：同一天（UTC 日）对同一个词重复提交**同一个** result 不再插行，
   返回 `duplicate=true`（防手抖/刷新重放）；同一天改答另一个 result 会插行
   （用户改口是真实信息，连击 streak 按最新一行算）。

时间一律 UTC ISO8601（`2026-08-18T04:05:06+00:00`，秒级），与 server/annotate 的
`_now()` 同款；解析时兼容不带时区的老行（按 UTC 解释）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from app.db import ENCOUNTER_SELECT, encounter_view

# --- 规则常量（DESIGN §7：M1 先滚动 + 会/不会，FSRS 以后再说） --------------

REVIEW_WINDOW_DAYS = 7  # 「最近几天滚动」的 N：added_at 在这个窗口内就进队
GRADUATE_STREAK = 2  # 连续几次 "know" 算毕业
GRADUATE_MIN_AGE_DAYS = 3  # 且最后一次复习距首次收藏至少几天
ONCE_PER_DAY = True  # 同一 UTC 日已复习过的词，今天不再出现

RESULT_KNOW = "know"
RESULT_DONT = "dont"
RESULTS = (RESULT_KNOW, RESULT_DONT)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def rules() -> dict[str, Any]:
    """当前规则口径，随 API 一起吐出去（前端/报告不用猜常量值）。"""
    return {
        "window_days": REVIEW_WINDOW_DAYS,
        "graduate_streak": GRADUATE_STREAK,
        "graduate_min_age_days": GRADUATE_MIN_AGE_DAYS,
        "once_per_day": ONCE_PER_DAY,
        "results": list(RESULTS),
    }


# --- 时间 ------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC ISO8601，秒级——与 server/annotate 的 _now() 完全一致。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(raw: str | None) -> datetime | None:
    """宽松解析时间戳：带 Z / 带偏移 / 裸时间（按 UTC 解释）都吃。解析不了返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # 老行/手写行没带时区：按 UTC 解释，不按本机时区
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_date(dt: datetime | None) -> date | None:
    return dt.astimezone(timezone.utc).date() if dt is not None else None


def _days_between(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds() / 86400.0


# --- 状态 ------------------------------------------------------------------


@dataclass
class EntryState:
    """一个 VocabEntry 的复习状态（全部由 Review 行推导，库里不存冗余字段）。"""

    id: int
    lexeme_id: int
    lemma: str
    pos: str | None
    ipa: str | None
    dict_gloss: str | None
    added_at: str
    note: str | None
    reviews: int = 0
    know_streak: int = 0
    last_at: str | None = None
    last_result: str | None = None
    ever_dont: bool = False
    reviewed_today: bool = False
    graduated: bool = False
    due: bool = False
    _added_dt: datetime | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "vocab_entry_id": self.id,
            "lexeme_id": self.lexeme_id,
            "lemma": self.lemma,
            "pos": self.pos,
            "ipa": self.ipa,
            "dict_gloss": self.dict_gloss,
            "added_at": self.added_at,
            "note": self.note,
            "reviews": self.reviews,
            "know_streak": self.know_streak,
            "last_reviewed_at": self.last_at,
            "last_result": self.last_result,
            "ever_dont": self.ever_dont,
            "reviewed_today": self.reviewed_today,
            "graduated": self.graduated,
            "due": self.due,
        }


def _entry_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT V.id, V.lexeme_id, V.added_at, V.note,"
        "       L.lemma, L.pos, L.ipa, L.dict_gloss "
        "FROM VocabEntry V JOIN Lexeme L ON L.id = V.lexeme_id "
        "ORDER BY V.id"
    ).fetchall()


def _review_rows(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
    """{vocab_entry_id: [(at, result), ...]}，按时间升序（同秒按 id 升序）。"""
    out: dict[int, list[tuple[str, str]]] = {}
    for r in conn.execute(
        "SELECT vocab_entry_id, at, result FROM Review ORDER BY vocab_entry_id, at, id"
    ):
        out.setdefault(int(r["vocab_entry_id"]), []).append((r["at"], r["result"]))
    return out


def _apply_history(
    state: EntryState, history: Sequence[tuple[str, str]], now: datetime
) -> None:
    today = utc_date(now)
    state.reviews = len(history)
    state.ever_dont = any(res == RESULT_DONT for _at, res in history)
    if history:
        state.last_at, state.last_result = history[-1]
    streak = 0
    for _at, res in reversed(history):
        if res != RESULT_KNOW:
            break
        streak += 1
    state.know_streak = streak

    last_dt = parse_ts(state.last_at)
    state.reviewed_today = any(utc_date(parse_ts(at)) == today for at, _res in history)

    age = _days_between(last_dt, state._added_dt)
    state.graduated = bool(
        streak >= GRADUATE_STREAK and age is not None and age >= GRADUATE_MIN_AGE_DAYS
    )

    # 「最近 N 天滚动」按 wall-clock 算：now - added_at ≤ N 天。
    # added_at 解析不出来（脏数据）时按「在窗口内」处理——宁可多复习一次，不丢词。
    added_age = _days_between(now, state._added_dt)
    in_window = added_age is None or added_age <= REVIEW_WINDOW_DAYS
    state.due = bool(
        not state.graduated
        and not (ONCE_PER_DAY and state.reviewed_today)
        and (in_window or state.ever_dont)
    )


def entry_states(
    conn: sqlite3.Connection, now: datetime | None = None
) -> list[EntryState]:
    """全部 VocabEntry 的复习状态（顺序 = VocabEntry.id 升序）。"""
    now = now or now_utc()
    history = _review_rows(conn)
    states: list[EntryState] = []
    for r in _entry_rows(conn):
        st = EntryState(
            id=int(r["id"]),
            lexeme_id=int(r["lexeme_id"]),
            lemma=r["lemma"],
            pos=r["pos"],
            ipa=r["ipa"],
            dict_gloss=r["dict_gloss"],
            added_at=r["added_at"],
            note=r["note"],
        )
        st._added_dt = parse_ts(st.added_at)
        _apply_history(st, history.get(st.id, []), now)
        states.append(st)
    return states


def _sort_key(st: EntryState) -> tuple:
    """从未复习的最优先，其次上次复习时间最早的优先；同档按收藏早、id 小。"""
    last = parse_ts(st.last_at)
    added = st._added_dt
    return (
        1 if last is not None else 0,  # 从未复习 → 0，排最前
        last.timestamp() if last is not None else 0.0,
        added.timestamp() if added is not None else 0.0,
        st.id,
    )


def due_states(
    conn: sqlite3.Connection, now: datetime | None = None
) -> list[EntryState]:
    return sorted((s for s in entry_states(conn, now) if s.due), key=_sort_key)


# --- 卡片装配（lexeme + gloss + ipa + 最近 encounter 原句 + 助记就绪） ------


def _latest_encounters(
    conn: sqlite3.Connection, entry_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """每个 entry 最近一条 encounter（按 Encounter.id 最大者）。

    来源可以是字幕段，也可以是网页划词（工单 11）——两种来源统一由
    `app.db.encounter_view` 铺平成同一套键名，本模块不关心它从哪来。
    """
    if not entry_ids:
        return {}
    marks = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        ENCOUNTER_SELECT + f"WHERE E.vocab_entry_id IN ({marks}) ORDER BY E.id",
        list(entry_ids),
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:  # 升序遍历，后写的覆盖前面的 → 留下 id 最大那条
        view = encounter_view(r)
        out[int(r["vocab_entry_id"])] = {
            "encounter_id": view["id"],
            "surface": view["surface"],
            "source_kind": view["source_kind"],
            "segment_id": view["segment_id"],
            "sentence": view["sentence"],
            "t_start": view["t_start"],
            "content_id": view["content_id"],
            "title": view["title"],
            "season_ep": view["season_ep"],
            "url": view["url"],
        }
    return out


def _mnemonic_status(
    conn: sqlite3.Connection, lexeme_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """助记就绪状态：与 /vocab 同口径（最新 job 状态 + 有没有 Mnemonic 行）。"""
    if not lexeme_ids:
        return {}
    marks = ",".join("?" * len(lexeme_ids))
    job_status = {
        int(r["lexeme_id"]): r["status"]
        for r in conn.execute(
            f"SELECT lexeme_id, status FROM AnnotationJob WHERE lexeme_id IN ({marks}) "
            "ORDER BY id",
            list(lexeme_ids),
        ).fetchall()
    }
    has_mnemonic = {
        int(r["lexeme_id"])
        for r in conn.execute(
            f"SELECT DISTINCT lexeme_id FROM Mnemonic WHERE lexeme_id IN ({marks})",
            list(lexeme_ids),
        ).fetchall()
    }
    return {
        lid: {
            "mnemonic_status": "done" if lid in has_mnemonic else job_status.get(lid, "none"),
            "has_mnemonic": lid in has_mnemonic,
        }
        for lid in set(int(i) for i in lexeme_ids)
    }


def cards(conn: sqlite3.Connection, states: Iterable[EntryState]) -> list[dict[str, Any]]:
    """状态 → 复习卡（lexeme + gloss + ipa + 最近原句 + 助记就绪状态）。"""
    states = list(states)
    encs = _latest_encounters(conn, [s.id for s in states])
    mnem = _mnemonic_status(conn, [s.lexeme_id for s in states])
    out = []
    for st in states:
        card = st.as_dict()
        card["encounter"] = encs.get(st.id)
        card.update(
            mnem.get(st.lexeme_id, {"mnemonic_status": "none", "has_mnemonic": False})
        )
        out.append(card)
    return out


# --- 答题 ------------------------------------------------------------------


class UnknownEntry(LookupError):
    """vocab_entry_id 不存在。"""


class BadResult(ValueError):
    """result 不是 know/dont。"""


def normalize_result(raw: str | None) -> str:
    r = (raw or "").strip().lower()
    if r not in RESULTS:
        raise BadResult(f"result 必须是 {'/'.join(RESULTS)}，收到 {raw!r}")
    return r


def _same_day_row(
    conn: sqlite3.Connection, vocab_entry_id: int, result: str, day: date
) -> sqlite3.Row | None:
    """今天（UTC）已经记过同样答案的那一行（幂等判据）。"""
    rows = conn.execute(
        "SELECT id, at, result FROM Review WHERE vocab_entry_id = ? AND result = ? "
        "ORDER BY id DESC LIMIT 50",
        (vocab_entry_id, result),
    ).fetchall()
    for r in rows:
        if utc_date(parse_ts(r["at"])) == day:
            return r
    return None


def answer(
    conn: sqlite3.Connection,
    vocab_entry_id: int,
    result: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """记一次「会 / 不会」，返回这张卡的最新状态。

    幂等：当天同一个 result 重复提交不再插行（duplicate=true）。
    """
    now = now or now_utc()
    today = utc_date(now)
    assert today is not None
    res = normalize_result(result)
    row = conn.execute(
        "SELECT id FROM VocabEntry WHERE id = ?", (vocab_entry_id,)
    ).fetchone()
    if row is None:
        raise UnknownEntry(f"vocab_entry {vocab_entry_id} 不存在")

    dup = _same_day_row(conn, vocab_entry_id, res, today)
    if dup is None:
        at = iso(now)
        with conn:
            conn.execute(
                "INSERT INTO Review (vocab_entry_id, at, result) VALUES (?,?,?)",
                (vocab_entry_id, at, res),
            )
        review_id, duplicate = None, False
    else:
        at, review_id, duplicate = dup["at"], int(dup["id"]), True

    states = entry_states(conn, now)
    state = next((s for s in states if s.id == vocab_entry_id), None)
    remaining = sum(1 for s in states if s.due)
    out: dict[str, Any] = {
        "vocab_entry_id": vocab_entry_id,
        "result": res,
        "at": at,
        "duplicate": duplicate,
        "recorded": not duplicate,
        "remaining": remaining,
    }
    if review_id is not None:
        out["review_id"] = review_id
    if state is not None:
        out.update(
            {
                "lemma": state.lemma,
                "reviews": state.reviews,
                "know_streak": state.know_streak,
                "graduated": state.graduated,
                "due": state.due,
            }
        )
    out["rules"] = rules()
    return out


# --- 统计 ------------------------------------------------------------------


def stats(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """今日已复习 / 待复习 / 毕业总数（UTC 日历日）。"""
    now = now or now_utc()
    today = utc_date(now)
    assert today is not None
    states = entry_states(conn, now)

    know_today = dont_today = 0
    for r in conn.execute("SELECT at, result FROM Review"):
        if utc_date(parse_ts(r["at"])) != today:
            continue
        if r["result"] == RESULT_KNOW:
            know_today += 1
        elif r["result"] == RESULT_DONT:
            dont_today += 1

    return {
        "date": today.isoformat(),
        "reviewed_today": sum(1 for s in states if s.reviewed_today),
        "know_today": know_today,
        "dont_today": dont_today,
        "due": sum(1 for s in states if s.due),
        "graduated": sum(1 for s in states if s.graduated),
        "total": len(states),
        "rules": rules(),
    }


def next_cards(
    conn: sqlite3.Connection,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """今日待复习卡列表 + remaining 计数（remaining = 队列总长，不受 limit 影响）。"""
    now = now or now_utc()
    limit = max(1, min(int(limit), MAX_LIMIT))
    due = due_states(conn, now)
    picked = due[:limit]
    return {
        "count": len(picked),
        "remaining": len(due),
        "limit": limit,
        "cards": cards(conn, picked),
        "rules": rules(),
    }
