"""SQLite schema + 连接管理（DESIGN.md §2）。

单文件即全部状态：一个 .db 装下内容、字幕、词元、生词本、助记队列。
所有连接开启外键约束；schema 用 IF NOT EXISTS，init_db 可重复调用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA_VERSION = 1

PathLike = Union[str, Path]

# --- DESIGN §2 的 9 张表 -------------------------------------------------
# M0 只用到 Content/Segment/Lexeme/WordForm/VocabEntry/Encounter/AnnotationJob，
# Mnemonic（M1）与 Review（M1）先建好，避免后续迁移。
SCHEMA = """
-- 1. 一集（或一个片段）的媒体 + 字幕来源
CREATE TABLE IF NOT EXISTS Content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    season_ep   TEXT NOT NULL,
    video_path  TEXT,
    srt_path    TEXT,
    UNIQUE (title, season_ep)
);

-- 2. 字幕段。tokens_json 供 GET /segments 直接吐给前端；
--    word_boxes_json 由 extract_hardsub.py 的词级包围盒回填（§4 热区）。
CREATE TABLE IF NOT EXISTS Segment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id      INTEGER NOT NULL REFERENCES Content(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    t_start         REAL NOT NULL,
    t_end           REAL NOT NULL,
    text_en         TEXT NOT NULL,
    tokens_json     TEXT,
    word_boxes_json TEXT,
    UNIQUE (content_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segment_content_time
    ON Segment (content_id, t_start);

-- 3. 客观词典条目缓存（来自 ECDICT），与用户行为无关
CREATE TABLE IF NOT EXISTS Lexeme (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma       TEXT NOT NULL UNIQUE,
    pos         TEXT,
    ipa         TEXT,
    dict_gloss  TEXT
);

-- 4. surface -> lexeme 映射；surface 统一小写
CREATE TABLE IF NOT EXISTS WordForm (
    surface     TEXT PRIMARY KEY,
    lexeme_id   INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_wordform_lexeme ON WordForm (lexeme_id);

-- 5. 用户收藏了什么
CREATE TABLE IF NOT EXISTS VocabEntry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id   INTEGER NOT NULL UNIQUE REFERENCES Lexeme(id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL,
    note        TEXT
);

-- 6. 每次真实语境下的相遇
CREATE TABLE IF NOT EXISTS Encounter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    segment_id      INTEGER NOT NULL REFERENCES Segment(id) ON DELETE CASCADE,
    surface         TEXT NOT NULL,
    added_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_encounter_vocab ON Encounter (vocab_entry_id);

-- 7. 异步助记任务队列（与收藏解耦）
CREATE TABLE IF NOT EXISTS AnnotationJob (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id   INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'done', 'failed')),
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_pick ON AnnotationJob (status, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_job_lexeme ON AnnotationJob (lexeme_id);

-- 8. 助记卡（按 lexeme 缓存 + 版本化）
CREATE TABLE IF NOT EXISTS Mnemonic (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id       INTEGER NOT NULL REFERENCES Lexeme(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    provider        TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    edited_by_user  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (lexeme_id, kind, version)
);

-- 9. 复习记录（M1 才写，表先建好）
CREATE TABLE IF NOT EXISTS Review (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    result          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_entry ON Review (vocab_entry_id, at);
"""

TABLES = (
    "Content",
    "Segment",
    "Lexeme",
    "WordForm",
    "VocabEntry",
    "Encounter",
    "AnnotationJob",
    "Mnemonic",
    "Review",
)


def get_conn(path: PathLike, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开数据库连接：行按名取、外键开启、WAL。

    不建表——建表走 init_db()。

    check_same_thread=False 只给"每线程一条连接 + 退出时统一 close"的场景用
    （app/server.py 的 Database）：sqlite3 不允许跨线程 close 一条 check 打开的
    连接，而进程退出必须能关掉所有线程的连接，否则 Windows 上 .db 文件被锁死。
    """
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(p) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: PathLike) -> sqlite3.Connection:
    """建库建表（幂等），返回已连接的 conn。"""
    conn = get_conn(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # executescript 会隐式 commit 并可能重置 pragma，这里复位外键开关
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="初始化 SQLite schema")
    ap.add_argument("--db", default="data/poi.db")
    args = ap.parse_args()
    conn = init_db(args.db)
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    print(f"{args.db}: {len(names)} tables -> {', '.join(names)}")
    conn.close()


if __name__ == "__main__":
    main()
