"""SQLite schema + 连接管理（DESIGN.md §2）。

单文件即全部状态：一个 .db 装下内容、字幕、词元、生词本、助记队列。
所有连接开启外键约束；schema 用 IF NOT EXISTS，init_db 可重复调用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Union

SCHEMA_VERSION = 2

PathLike = Union[str, Path]

# Encounter 的列定义单独拎出来：建库（SCHEMA）与老库迁移（_migrate_encounter）
# 必须逐字用同一份 DDL，否则新库老库长得不一样，是最难查的那种 bug。
ENCOUNTER_BODY = """(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vocab_entry_id  INTEGER NOT NULL REFERENCES VocabEntry(id) ON DELETE CASCADE,
    segment_id      INTEGER REFERENCES Segment(id) ON DELETE CASCADE,
    surface         TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    source_kind     TEXT NOT NULL DEFAULT 'segment'
                    CHECK (source_kind IN ('segment', 'web')),
    context_json    TEXT
)"""

SOURCE_SEGMENT = "segment"
SOURCE_WEB = "web"

# --- DESIGN §2 的 9 张表 -------------------------------------------------
# M0 只用到 Content/Segment/Lexeme/WordForm/VocabEntry/Encounter/AnnotationJob，
# Mnemonic（M1）与 Review（M1）先建好，避免后续迁移。
SCHEMA = f"""
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

-- 6. 每次真实语境下的相遇（v2 泛化：不只来自字幕段）
--    source_kind='segment' → segment_id 指字幕段，语境从 Segment/Content 取；
--    source_kind='web'     → segment_id 为 NULL，语境存 context_json
--                            {{url, title, sentence}}（浏览器划词插件，工单 11）。
CREATE TABLE IF NOT EXISTS Encounter {ENCOUNTER_BODY};
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


class ConnRegistry:
    """可枚举的连接登记簿：每线程一条连接 → 登记 → 统一关闭。线程安全。

    （工单 9：原居 app/server.py 的 `_ConnRegistry`，因 EcdictStore 抽到
    app/ecdict.py 而下沉到这里——连接管理本来就是 db 层的事。）

    每线程一条的代价：threading.local 只够本线程自己关，进程要退出时够不着别的
    线程那些连接 —— Windows 上就表现为 .db 文件被锁住、删不掉也重建不了（工单 6-4）。
    所以每开一条连接都往**显式注册表**里登记（sqlite3.Connection 不支持弱引用，
    只能用强引用列表；连接数上限 = 线程池大小，不会涨飞）。close_all() 时统一关掉：
    sqlite3 允许跨线程 close，前提是连接开的时候带 check_same_thread=False。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []

    def _register(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        with self._lock:
            self._conns.append(conn)
        return conn

    def close_all(self) -> int:
        """关掉本对象开过的所有连接（含别的线程开的），返回关掉几条。

        换掉 threading.local 实例本身，等于一次性丢掉**所有**线程缓存的引用；
        之后哪个线程再来取连接都会重开一条，对象因此可以继续用。
        """
        with self._lock:
            conns, self._conns = self._conns, []
            self._local = threading.local()
        n = 0
        for c in conns:
            try:
                c.close()
                n += 1
            except sqlite3.Error:
                pass
        return n

    def _cached(self) -> sqlite3.Connection | None:
        return getattr(self._local, "conn", None)

    def _cache(self, conn: sqlite3.Connection | None) -> None:
        self._local.conn = conn


class Database(ConnRegistry):
    """poi.db 的每线程连接池。首次取连接时建表（幂等），import 阶段不碰磁盘。"""

    def __init__(self, path: PathLike) -> None:
        super().__init__()
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._ready = False

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._schema_lock:
            if not self._ready:
                init_db(self.path).close()
                self._ready = True

    def conn(self) -> sqlite3.Connection:
        c = self._cached()
        if c is None:
            self._ensure_schema()
            # check_same_thread=False：连接仍然只给开它的那个线程用，
            # 放开只是为了 close_all() 能在退出时跨线程关掉它（工单 6-4）
            c = self._register(get_conn(self.path, check_same_thread=False))
            self._cache(c)
        return c


# --- 语境读取口径（/vocab 与 /review/next 共用，见工单 11） -----------------

# Encounter 一行的完整语境：字幕来源要 JOIN 出剧集信息，web 来源全在 context_json 里。
# LEFT JOIN 是关键：web 行的 segment_id 是 NULL，INNER JOIN 会把它整行吃掉。
ENCOUNTER_SELECT = (
    "SELECT E.id, E.vocab_entry_id, E.surface, E.added_at, E.segment_id,"
    "       E.source_kind, E.context_json,"
    "       S.text_en, S.t_start, S.content_id, C.title, C.season_ep "
    "FROM Encounter E "
    "LEFT JOIN Segment S ON S.id = E.segment_id "
    "LEFT JOIN Content C ON C.id = S.content_id "
)


def encounter_view(row: sqlite3.Row) -> dict[str, Any]:
    """Encounter 行（按 ENCOUNTER_SELECT 取）→ 前端字段。两种来源同一套键名。

    - segment 来源：sentence/t_start/content_id/title/season_ep 来自 Segment/Content，
      url 为 None（前端据此给「去这句」按钮）。
    - web 来源：sentence/url/title 来自 context_json，时间轴相关字段全 None。
    """
    kind = row["source_kind"] if "source_kind" in row.keys() else SOURCE_SEGMENT
    kind = kind or SOURCE_SEGMENT
    ctx: dict[str, Any] = {}
    if kind == SOURCE_WEB:
        raw = row["context_json"]
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    ctx = loaded
            except (ValueError, TypeError):
                ctx = {}
    return {
        "id": row["id"],
        "surface": row["surface"],
        "added_at": row["added_at"],
        "source_kind": kind,
        "segment_id": row["segment_id"],
        "sentence": ctx.get("sentence") if kind == SOURCE_WEB else row["text_en"],
        "t_start": None if kind == SOURCE_WEB else row["t_start"],
        "content_id": None if kind == SOURCE_WEB else row["content_id"],
        "title": ctx.get("title") if kind == SOURCE_WEB else row["title"],
        "season_ep": None if kind == SOURCE_WEB else row["season_ep"],
        "url": ctx.get("url") if kind == SOURCE_WEB else None,
    }


# --- 迁移 ------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _migrate_encounter(conn: sqlite3.Connection) -> bool:
    """v1 → v2：Encounter 泛化（segment_id 可空 + source_kind + context_json）。

    SQLite 改不了列的 NOT NULL 约束，只能整表重建（官方 12 步法的精简版）：
    建新表 → 搬数据 → 删旧表 → 改名。新表 DDL 与建库共用 ENCOUNTER_BODY，
    保证"老库迁上来"和"新库直接建"长得一模一样。

    幂等：已经有 source_kind 列（或压根没有 Encounter 表）就直接返回 False。
    老数据全部按 source_kind='segment' 落位——历史 encounter 本来就来自字幕段。
    """
    if not _table_exists(conn, "Encounter"):
        return False
    if "source_kind" in _columns(conn, "Encounter"):
        return False

    # 重建期间必须关外键：DROP TABLE 会先让子表引用悬空。
    # PRAGMA foreign_keys 在事务里改无效，所以先提交干净再动。
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute(f"CREATE TABLE Encounter_migrating {ENCOUNTER_BODY}")
            conn.execute(
                "INSERT INTO Encounter_migrating "
                "(id, vocab_entry_id, segment_id, surface, added_at, source_kind) "
                f"SELECT id, vocab_entry_id, segment_id, surface, added_at, '{SOURCE_SEGMENT}' "
                "FROM Encounter"
            )
            conn.execute("DROP TABLE Encounter")
            conn.execute("ALTER TABLE Encounter_migrating RENAME TO Encounter")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:  # 老库本来就有孤儿行才可能触发；宁可炸也不静默留脏数据
            raise sqlite3.IntegrityError(f"Encounter 迁移后外键不自洽: {bad[:3]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def init_db(path: PathLike) -> sqlite3.Connection:
    """建库建表（幂等），返回已连接的 conn。老库自动迁移到当前 SCHEMA_VERSION。"""
    conn = get_conn(path)
    _migrate_encounter(conn)
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
