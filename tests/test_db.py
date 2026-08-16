"""schema 完整性、外键约束、连接行为（DESIGN §2）。"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import SCHEMA_VERSION, TABLES, get_conn, init_db


def table_names(conn):
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_all_nine_tables_created(conn):
    assert table_names(conn) == set(TABLES)
    assert len(TABLES) == 9


def test_init_db_is_idempotent(db_path):
    c1 = init_db(db_path)
    c1.execute("INSERT INTO Content (title, season_ep) VALUES ('T', 's01e01')")
    c1.commit()
    c1.close()
    c2 = init_db(db_path)
    assert table_names(c2) == set(TABLES)
    assert c2.execute("SELECT COUNT(*) c FROM Content").fetchone()["c"] == 1
    assert c2.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    c2.close()


def test_foreign_keys_pragma_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert get_conn(":memory:").execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_key_violation_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO Segment (content_id, idx, t_start, t_end, text_en) "
            "VALUES (9999, 1, 0.0, 1.0, 'orphan')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO WordForm (surface, lexeme_id) VALUES ('x', 4242)")


def test_cascade_delete_content_removes_segments(conn):
    cur = conn.execute("INSERT INTO Content (title, season_ep) VALUES ('T','s01e01')")
    cid = cur.lastrowid
    conn.execute(
        "INSERT INTO Segment (content_id, idx, t_start, t_end, text_en) "
        "VALUES (?,1,0.0,1.0,'hello')",
        (cid,),
    )
    conn.execute("DELETE FROM Content WHERE id = ?", (cid,))
    assert conn.execute("SELECT COUNT(*) c FROM Segment").fetchone()["c"] == 0


def test_unique_constraints(conn):
    conn.execute("INSERT INTO Content (title, season_ep) VALUES ('T','s01e01')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO Content (title, season_ep) VALUES ('T','s01e01')")
    conn.rollback()

    conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
    conn.rollback()


def test_annotation_job_status_check(conn):
    conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')")
    lid = conn.execute("SELECT id FROM Lexeme").fetchone()["id"]
    conn.execute(
        "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
        "VALUES (?, 'queued', 10, '2026-08-16T00:00:00Z')",
        (lid,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO AnnotationJob (lexeme_id, status, created_at) "
            "VALUES (?, 'bogus', '2026-08-16T00:00:00Z')",
            (lid,),
        )


def test_full_collect_chain_inserts(conn):
    """/collect 走通的链路：Lexeme → VocabEntry → Encounter，外键都成立。"""
    cid = conn.execute(
        "INSERT INTO Content (title, season_ep) VALUES ('T','s01e01')"
    ).lastrowid
    sid = conn.execute(
        "INSERT INTO Segment (content_id, idx, t_start, t_end, text_en) "
        "VALUES (?,1,0.0,1.0,'a stakeout tonight')",
        (cid,),
    ).lastrowid
    lid = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')").lastrowid
    vid = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-08-16T00:00:00Z')",
        (lid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
        "VALUES (?,?,?,'2026-08-16T00:00:00Z')",
        (vid, sid, "stakeout"),
    )
    conn.execute(
        "INSERT INTO Review (vocab_entry_id, at, result) "
        "VALUES (?, '2026-08-16T00:00:00Z', 'ok')",
        (vid,),
    )
    conn.execute(
        "INSERT INTO Mnemonic (lexeme_id, kind, payload_json, provider) "
        "VALUES (?, 'pun', '{}', 'anthropic')",
        (lid,),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM Encounter").fetchone()["c"] == 1
    # 删除生词条目，encounter/review 跟着走
    conn.execute("DELETE FROM VocabEntry WHERE id = ?", (vid,))
    assert conn.execute("SELECT COUNT(*) c FROM Encounter").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM Review").fetchone()["c"] == 0
