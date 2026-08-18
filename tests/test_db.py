"""schema 完整性、外键约束、连接行为（DESIGN §2）。"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import (
    ENCOUNTER_SELECT,
    SCHEMA_VERSION,
    TABLES,
    encounter_view,
    get_conn,
    init_db,
)


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


# --- v1 → v2 迁移：Encounter 泛化（工单 11） --------------------------------

# v1 的老库长这样：Encounter.segment_id 是 NOT NULL，没有 source_kind/context_json。
# 迁移测试必须拿**真的老 DDL** 建库，不能拿新 SCHEMA 改改凑合。
SCHEMA_V1_ENCOUNTER = """
CREATE TABLE Encounter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    segment_id      INTEGER NOT NULL REFERENCES Segment(id) ON DELETE CASCADE,
    surface         TEXT NOT NULL,
    added_at        TEXT NOT NULL
);
CREATE INDEX idx_encounter_vocab ON Encounter (vocab_entry_id);
"""


def make_v1_db(path) -> None:
    """造一个 v1 老库：schema 用老 DDL，塞两条真实链路的数据。"""
    conn = get_conn(path)
    with conn:
        # 新 SCHEMA 里除 Encounter 外的表结构 v1/v2 没变，直接借用
        from app.db import SCHEMA

        head, _, tail = SCHEMA.partition("-- 6. ")
        _, _, rest = tail.partition("-- 7. ")
        conn.executescript(head + SCHEMA_V1_ENCOUNTER + "-- 7. " + rest)
        conn.execute("PRAGMA user_version = 1")
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
        conn.executemany(
            "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
            "VALUES (?,?,?,?)",
            [
                (vid, sid, "stakeout", "2026-08-16T00:00:00Z"),
                (vid, sid, "stakeouts", "2026-08-17T00:00:00Z"),
            ],
        )
    conn.close()


def test_v1_db_really_lacks_new_columns(db_path):
    """先证明夹具是老库：不然下面的迁移测试等于测了个寂寞。"""
    make_v1_db(db_path)
    conn = get_conn(db_path)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(Encounter)")}
    assert "source_kind" not in cols and "context_json" not in cols
    assert cols["segment_id"][3] == 1  # notnull
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_migration_keeps_old_rows_and_adds_columns(db_path):
    make_v1_db(db_path)
    conn = init_db(db_path)

    rows = conn.execute("SELECT * FROM Encounter ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2]
    assert [r["surface"] for r in rows] == ["stakeout", "stakeouts"]
    assert [r["added_at"] for r in rows] == [
        "2026-08-16T00:00:00Z",
        "2026-08-17T00:00:00Z",
    ]
    # 老数据一律算字幕来源，segment_id 原样保留
    assert {r["source_kind"] for r in rows} == {"segment"}
    assert all(r["segment_id"] == 1 for r in rows)
    assert all(r["context_json"] is None for r in rows)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # 别的表没被误伤
    assert conn.execute("SELECT COUNT(*) c FROM Segment").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM VocabEntry").fetchone()["c"] == 1
    assert table_names(conn) == set(TABLES)
    conn.close()


def test_migrated_db_matches_fresh_db_ddl(db_path, tmp_path):
    """迁上来的库和新建的库必须一模一样：列名、类型、非空、默认值全对齐。"""
    make_v1_db(db_path)
    migrated = init_db(db_path)
    fresh = init_db(tmp_path / "fresh.db")
    got = [tuple(r)[1:] for r in migrated.execute("PRAGMA table_info(Encounter)")]
    want = [tuple(r)[1:] for r in fresh.execute("PRAGMA table_info(Encounter)")]
    assert got == want
    # 索引也得在（DROP TABLE 会带走旧索引）
    idx = {
        r["name"]
        for r in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='Encounter'"
        )
    }
    assert "idx_encounter_vocab" in idx
    migrated.close()
    fresh.close()


def test_migration_is_idempotent_and_preserves_web_rows(db_path):
    make_v1_db(db_path)
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at,"
        " source_kind, context_json) VALUES (1, NULL, 'stakeout', '2026-08-18T00:00:00Z',"
        " 'web', '{\"url\":\"https://example.com/a\",\"title\":\"T\",\"sentence\":\"S\"}')"
    )
    conn.commit()
    conn.close()

    again = init_db(db_path)  # 再迁一次：不该动任何东西
    rows = again.execute("SELECT * FROM Encounter ORDER BY id").fetchall()
    assert len(rows) == 3
    assert rows[2]["source_kind"] == "web" and rows[2]["segment_id"] is None
    assert "example.com" in rows[2]["context_json"]
    again.close()


def test_web_encounter_needs_no_segment_and_kind_is_checked(conn):
    lid = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')").lastrowid
    vid = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-08-18T00:00:00Z')",
        (lid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at,"
        " source_kind, context_json) VALUES (?, NULL, 'x', '2026-08-18T00:00:00Z',"
        " 'web', '{}')",
        (vid,),
    )
    # 默认值仍是 segment（老代码不写 source_kind 也不会炸）
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
        "VALUES (?, NULL, 'y', '2026-08-18T00:00:00Z')",
        (vid,),
    )
    assert conn.execute(
        "SELECT COUNT(*) c FROM Encounter WHERE source_kind = 'segment'"
    ).fetchone()["c"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at,"
            " source_kind) VALUES (?, NULL, 'z', '2026-08-18T00:00:00Z', 'twitter')",
            (vid,),
        )


def test_encounter_view_flattens_both_sources(conn):
    """/vocab 与 /review/next 共用的取数口径：两种来源同一套键名。"""
    cid = conn.execute(
        "INSERT INTO Content (title, season_ep) VALUES ('Fixture Show','s01e01')"
    ).lastrowid
    sid = conn.execute(
        "INSERT INTO Segment (content_id, idx, t_start, t_end, text_en) "
        "VALUES (?,1,2.5,4.0,'The gardener began a stakeout.')",
        (cid,),
    ).lastrowid
    lid = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')").lastrowid
    vid = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-08-18T00:00:00Z')",
        (lid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
        "VALUES (?,?,'stakeout','2026-08-18T00:00:00Z')",
        (vid, sid),
    )
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at,"
        " source_kind, context_json) VALUES (?, NULL, 'stakeouts',"
        " '2026-08-18T01:00:00Z', 'web',"
        " '{\"url\":\"https://example.com/a\",\"title\":\"Example\","
        "\"sentence\":\"Two stakeouts went sideways.\"}')",
        (vid,),
    )
    conn.commit()
    rows = conn.execute(ENCOUNTER_SELECT + "ORDER BY E.id").fetchall()
    seg, web = [encounter_view(r) for r in rows]

    assert seg["source_kind"] == "segment"
    assert seg["sentence"] == "The gardener began a stakeout."
    assert (seg["season_ep"], seg["t_start"], seg["content_id"]) == ("s01e01", 2.5, cid)
    assert seg["url"] is None

    assert web["source_kind"] == "web"
    assert web["sentence"] == "Two stakeouts went sideways."
    assert web["title"] == "Example" and web["url"] == "https://example.com/a"
    assert web["segment_id"] is None
    assert (web["t_start"], web["content_id"], web["season_ep"]) == (None, None, None)
    assert set(seg) == set(web)  # 键名必须完全一致，前端才能一套代码画两种来源


def test_encounter_view_survives_broken_context_json(conn):
    lid = conn.execute("INSERT INTO Lexeme (lemma) VALUES ('stakeout')").lastrowid
    vid = conn.execute(
        "INSERT INTO VocabEntry (lexeme_id, added_at) VALUES (?, '2026-08-18T00:00:00Z')",
        (lid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at,"
        " source_kind, context_json) VALUES (?, NULL, 'x', '2026-08-18T00:00:00Z',"
        " 'web', 'not json{')",
        (vid,),
    )
    conn.commit()
    view = encounter_view(conn.execute(ENCOUNTER_SELECT + "LIMIT 1").fetchone())
    assert view["sentence"] is None and view["url"] is None
