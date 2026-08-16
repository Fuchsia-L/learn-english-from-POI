"""scripts/prefetch.py：当集 lemma ∩ 词表 → 词频降序 → 低优先级入队。

词表用自造小词表（不含真实 cet46 数据），字幕复用 conftest 的自造 srt。
wordfreq 是离线词频表，本文件不发任何网络请求。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prefetch  # noqa: E402

from app.db import get_conn  # noqa: E402
from app.ingest import ingest_srt  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402

# 自造词表：前 5 个真的出现在 FIXTURE_SRT 里，后 2 个不出现
WORDS = ["home", "gardener", "cousin", "camera", "believe", "stakeout", "helicopter"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class Boom(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError("prefetch 不应发起任何网络调用")

    monkeypatch.setattr(socket, "socket", Boom)


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    srt = tmp_path / "fixture.srt"
    srt.write_text(FIXTURE_SRT, encoding="utf-8")
    db = tmp_path / "poi.db"
    stats = ingest_srt(db_path=db, srt_path=srt, title="Test Show", season_ep="s01e01")
    wl = tmp_path / "wordlist.txt"
    wl.write_text("# 自造词表\n" + "\n".join(WORDS) + "\n\n", encoding="utf-8")
    return {"db": db, "wordlist": wl, "content_id": stats["content_id"]}


def jobs(db: Path) -> list[tuple[str, str, int]]:
    conn = get_conn(db)
    rows = conn.execute(
        "SELECT L.lemma, J.status, J.priority FROM AnnotationJob J "
        "JOIN Lexeme L ON L.id = J.lexeme_id ORDER BY J.id"
    ).fetchall()
    conn.close()
    return [(r["lemma"], r["status"], r["priority"]) for r in rows]


# --- 词表解析 --------------------------------------------------------------


def test_load_wordlist_strips_comments_and_normalizes(tmp_path: Path):
    p = tmp_path / "wl.txt"
    p.write_text("Home\n# 注释\n\n  COUSIN  \nrain # 行尾注释\n", encoding="utf-8")
    assert prefetch.load_wordlist(p) == {"home", "cousin", "rain"}


def test_missing_wordlist_exits_nonzero_with_hint(env: dict, capsys):
    rc = prefetch.main(
        ["--db", str(env["db"]), "--content-id", "1", "--wordlist", "nope/cet46.txt"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "不存在" in err and "cet46" in err and "ecdict" in err


def test_empty_wordlist_exits_nonzero(env: dict, tmp_path: Path, capsys):
    empty = tmp_path / "empty.txt"
    empty.write_text("# 只有注释\n", encoding="utf-8")
    rc = prefetch.main(
        ["--db", str(env["db"]), "--content-id", "1", "--wordlist", str(empty)]
    )
    assert rc == 2 and "空的" in capsys.readouterr().err


def test_unknown_content_exits_nonzero(env: dict, capsys):
    rc = prefetch.main(
        ["--db", str(env["db"]), "--content-id", "999",
         "--wordlist", str(env["wordlist"])]
    )
    assert rc == 2 and "不存在" in capsys.readouterr().err


# --- 入队 ------------------------------------------------------------------


def test_enqueues_intersection_at_low_priority(env: dict):
    stats = prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]), log=lambda _m: None
    )
    # camera 在字幕里是 "cameras"，lemma 归一后才对得上；stakeout/helicopter 不在这集
    assert set(stats["queued_lemmas"]) == {"home", "gardener", "cousin", "camera", "believe"}
    assert stats["matched"] == 5 and stats["skipped_existing"] == 0
    assert all(status == "queued" and pri == 0 for _l, status, pri in jobs(env["db"]))


def test_ordered_by_word_frequency_desc(env: dict):
    stats = prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]), log=lambda _m: None
    )
    from wordfreq import zipf_frequency

    freqs = [zipf_frequency(w, "en") for w in stats["queued_lemmas"]]
    assert freqs == sorted(freqs, reverse=True)
    assert stats["queued_lemmas"][0] == "home"  # 最常见的排头
    assert stats["queued_lemmas"][-1] == "gardener"


def test_limit_truncates_by_frequency(env: dict):
    stats = prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]),
        limit=2, log=lambda _m: None,
    )
    assert stats["queued"] == 2
    assert stats["queued_lemmas"] == ["home", "believe"]


def test_rerun_is_idempotent(env: dict):
    words = prefetch.load_wordlist(env["wordlist"])
    first = prefetch.prefetch(env["db"], env["content_id"], words, log=lambda _m: None)
    second = prefetch.prefetch(env["db"], env["content_id"], words, log=lambda _m: None)
    assert second["queued"] == 0
    assert second["skipped_existing"] == first["queued"] == 5
    assert len(jobs(env["db"])) == 5


def test_skips_done_and_queued_but_retries_failed(env: dict):
    words = prefetch.load_wordlist(env["wordlist"])
    prefetch.prefetch(env["db"], env["content_id"], words, log=lambda _m: None)
    conn = get_conn(env["db"])
    with conn:
        conn.execute(
            "UPDATE AnnotationJob SET status='done' WHERE lexeme_id = "
            "(SELECT id FROM Lexeme WHERE lemma='home')"
        )
        conn.execute(
            "UPDATE AnnotationJob SET status='failed' WHERE lexeme_id = "
            "(SELECT id FROM Lexeme WHERE lemma='cousin')"
        )
    conn.close()
    stats = prefetch.prefetch(env["db"], env["content_id"], words, log=lambda _m: None)
    assert stats["queued_lemmas"] == ["cousin"]  # 只有失败过的重排


def test_dry_run_writes_nothing(env: dict):
    stats = prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]),
        dry_run=True, log=lambda _m: None,
    )
    assert stats["queued"] == 5 and jobs(env["db"]) == []


def test_collected_word_keeps_high_priority(env: dict):
    """收藏过的词已有高优先级任务，预热不许把它挤成低优先级（DESIGN §5）。"""
    conn = get_conn(env["db"])
    with conn:
        lex = conn.execute("SELECT id FROM Lexeme WHERE lemma='home'").fetchone()
        conn.execute(
            "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
            "VALUES (?, 'queued', 10, '2026-01-01T00:00:00+00:00')",
            (int(lex["id"]),),
        )
    conn.close()
    prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]), log=lambda _m: None
    )
    got = [j for j in jobs(env["db"]) if j[0] == "home"]
    assert got == [("home", "queued", 10)]


# --- CLI -------------------------------------------------------------------


def test_cli_prints_stats(env: dict, capsys):
    rc = prefetch.main(
        ["--db", str(env["db"]), "--content-id", str(env["content_id"]),
         "--wordlist", str(env["wordlist"]), "--limit", "3"]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "入队(priority=0) : 3" in out and "词频降序前几个" in out
    assert len(jobs(env["db"])) == 3


def test_cli_dry_run_leaves_db_untouched(env: dict, capsys):
    rc = prefetch.main(
        ["--db", str(env["db"]), "--content-id", str(env["content_id"]),
         "--wordlist", str(env["wordlist"]), "--dry-run"]
    )
    assert rc == 0 and "dry-run" in capsys.readouterr().out
    assert jobs(env["db"]) == []


# --- 与 worker 串起来 ------------------------------------------------------


def test_prefetched_jobs_are_processed_by_worker(env: dict):
    """预热入队 → worker 消费 → Mnemonic 落库（离线全链路）。"""
    from app.annotate import AnnotateWorker
    from app.providers.fake import FakeProvider

    prefetch.prefetch(
        env["db"], env["content_id"], prefetch.load_wordlist(env["wordlist"]),
        limit=2, log=lambda _m: None,
    )
    with AnnotateWorker(
        db_path=env["db"], provider=FakeProvider(), log=lambda _m: None
    ) as w:
        stats = w.run_once()
    assert stats.done == 2
    conn = get_conn(env["db"])
    n = conn.execute("SELECT COUNT(*) c FROM Mnemonic").fetchone()["c"]
    conn.close()
    assert n >= 2
