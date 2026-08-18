"""FastAPI 服务：API + Range 媒体接口 + 静态托管 player（DESIGN.md §3 §4）。

启动:
    uvicorn app.server:app                      # 读环境变量 POI_DB / POI_ECDICT
    python -m app.server --db data/poi.db --ecdict data/ecdict.db --port 8000

约定（与 db.py / ingest.py / build_ecdict.py 对齐）:
- 一切查询走 SQLite，本模块**不做任何网络调用**（LLM 由 annotate worker 负责）。
- surface 一律小写归一（ingest.normalize_surface），WordForm 主键即小写 surface。
- Lexeme 是客观词典缓存：ingest 只建骨架行（pos/ipa/dict_gloss 为 NULL），
  首次 /lookup 或 /collect 时从 ecdict.db 回填。
- ECDICT 查询/回填口径住在 **app/ecdict.py**（工单 9 抽层）：server 与 annotate
  worker 共用同一份实现，worker 不再反向依赖 web 层。
- 释义里的字面 "\\n" 分隔符原样吐给前端，服务端不折行。
- ecdict.db 不存在/损坏时优雅降级：in_dict=false，不 500。
- 连接每线程一条并登记在册（app/db.py 的 Database/ConnRegistry），
  lifespan 退出时统一 close（Windows 不锁库文件）。
- 复习闭环（M1）：规则与 SQL 住在 **app/review.py**（纯 SQLite），本模块只做
  HTTP 壳子 —— /review/next、/review/answer、/review/stats。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import review as review_rules
from app.consts import COLLECT_JOB_PRIORITY, DEFAULT_DB, DEFAULT_ECDICT
from app.db import Database
from app.ecdict import EcdictStore, fill_from_ecdict
from app.ingest import lemmatize, normalize_surface

MEDIA_CHUNK = 64 * 1024
# 单 Range：`bytes=0-1023` / `bytes=1024-` / `bytes=-500`；多 Range 只取第一段
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Lexeme 词典字段回填（口径见 app/ecdict.py） ---------------------------


def _lexeme_row(conn: sqlite3.Connection, lexeme_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, lemma, pos, ipa, dict_gloss FROM Lexeme WHERE id = ?", (lexeme_id,)
    ).fetchone()


def _resolve_surface(
    conn: sqlite3.Connection, surface: str
) -> tuple[str, sqlite3.Row | None]:
    """小写 surface → (lemma, Lexeme 行或 None)。只读，不建行。"""
    row = conn.execute(
        "SELECT L.id, L.lemma, L.pos, L.ipa, L.dict_gloss FROM WordForm W "
        "JOIN Lexeme L ON L.id = W.lexeme_id WHERE W.surface = ?",
        (surface,),
    ).fetchone()
    if row is not None:
        return row["lemma"], row
    lemma = lemmatize(surface)
    row = conn.execute(
        "SELECT id, lemma, pos, ipa, dict_gloss FROM Lexeme WHERE lemma = ?", (lemma,)
    ).fetchone()
    return lemma, row


def _is_collected(conn: sqlite3.Connection, lexeme_id: int | None) -> bool:
    if lexeme_id is None:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM VocabEntry WHERE lexeme_id = ?", (lexeme_id,)
        ).fetchone()
        is not None
    )


def _clean_surface(raw: str) -> str:
    s = normalize_surface(raw or "").strip()
    return s.strip("\"'“”‘’.,!?;:()[]{}…-—–")


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# --- Range 解析 ------------------------------------------------------------


class RangeSpec(BaseModel):
    start: int
    end: int  # 闭区间，含

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range(header: str | None, size: int) -> tuple[str, RangeSpec | None]:
    """解析 Range 头。

    返回 ('full'|'partial'|'unsatisfiable', spec)。
    - 语法非法 / 非 bytes 单位 → 'full'（RFC 9110：无法理解的 Range 必须忽略）
    - first-byte-pos 越界、suffix-length=0、空文件 → 'unsatisfiable'（416）
    - 多 Range 只取第一段（M0 用不到 multipart/byteranges）
    """
    if not header:
        return "full", None
    m = _RANGE_RE.match(header)
    if not m:
        return "full", None
    raw_start, raw_end = m.group(1), m.group(2)
    if raw_start == "" and raw_end == "":
        return "full", None

    if raw_start == "":  # 后缀区间 bytes=-N
        suffix = int(raw_end)
        if suffix == 0 or size == 0:
            return "unsatisfiable", None
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(raw_start)
        if size == 0 or start >= size:
            return "unsatisfiable", None
        end = size - 1 if raw_end == "" else min(int(raw_end), size - 1)
        if end < start:
            return "full", None  # 语法上无效的区间 → 忽略 Range
    return "partial", RangeSpec(start=start, end=end)


def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(MEDIA_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed and guessed.split("/")[0] in ("video", "audio"):
        return guessed
    return "video/mp4"  # <video> 需要一个可播的类型，未知后缀按 mp4 试


# --- 请求体 ----------------------------------------------------------------


class CollectIn(BaseModel):
    surface: str = Field(..., min_length=1)
    segment_id: int
    note: str | None = None


class ReviewAnswerIn(BaseModel):
    """POST /review/answer 的请求体。result 的合法值校验放在 app/review.py
    （规则归规则层），这里只要求非空字符串 —— 非法值回 400 而不是 422。"""

    vocab_entry_id: int = Field(..., ge=1)
    result: str = Field(..., min_length=1)


# --- 应用 -------------------------------------------------------------------


def create_app(
    db_path: str | Path | None = None,
    ecdict_path: str | Path | None = None,
) -> FastAPI:
    """建 app。参数优先，其次环境变量 POI_DB / POI_ECDICT，最后默认值。"""
    db_file = Path(db_path or os.environ.get("POI_DB") or DEFAULT_DB)
    ecdict_file = Path(ecdict_path or os.environ.get("POI_ECDICT") or DEFAULT_ECDICT)

    # 建表在首个请求触发（Database._ensure_schema，幂等）——import 阶段不碰磁盘
    db = Database(db_file)
    ecdict = EcdictStore(ecdict_file)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """进程退出时把两个库的**所有线程**连接都关掉（工单 6-4）。

        不关的后果在 Windows 上是实锤的：uvicorn 停了，poi.db / ecdict.db 仍被
        锁着，用户删不掉也重建不了词典。Linux 上删得掉，但 WAL 文件照样残留。
        """
        yield
        closed = db.close_all() + ecdict.close_all()
        if closed:
            print(f"[server] 关闭 {closed} 条 SQLite 连接", flush=True)

    app = FastAPI(
        title="learn-english-from-POI",
        version="0.2",
        description="本地看剧学词服务：字幕点词 → 查词 → 收藏 → 助记（DESIGN.md §3）",
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.ecdict = ecdict
    app.state.db_path = db_file
    app.state.ecdict_path = ecdict_file

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    # player.html 还没落地也不影响挂载（check_dir=False 兜底目录被删的情况）
    app.mount("/static", StaticFiles(directory=str(static_dir), check_dir=False), name="static")

    # ---- 根路径 -----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/static/player.html")

    # ---- GET /episodes ----------------------------------------------------

    @app.get("/episodes")
    def episodes() -> dict:
        conn = db.conn()
        rows = conn.execute(
            "SELECT C.id, C.title, C.season_ep, C.video_path, C.srt_path,"
            "       COUNT(S.id) AS n_segments, COALESCE(MAX(S.t_end), 0) AS duration "
            "FROM Content C LEFT JOIN Segment S ON S.content_id = C.id "
            "GROUP BY C.id ORDER BY C.title, C.season_ep"
        ).fetchall()
        out = []
        for r in rows:
            video_path = r["video_path"]
            out.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "season_ep": r["season_ep"],
                    "segments": r["n_segments"],
                    "duration": round(float(r["duration"]), 3),
                    "has_video": bool(video_path) and Path(video_path).exists(),
                    "media_url": f"/media/{r['id']}",
                }
            )
        return {"episodes": out}

    # ---- GET /media/{content_id} （Range） --------------------------------

    @app.api_route("/media/{content_id}", methods=["GET", "HEAD"])
    def media(content_id: int, request: Request) -> Response:
        conn = db.conn()
        row = conn.execute(
            "SELECT video_path FROM Content WHERE id = ?", (content_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"content {content_id} 不存在")
        if not row["video_path"]:
            raise HTTPException(
                status_code=404, detail=f"content {content_id} 没有登记 video_path"
            )
        path = Path(row["video_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"媒体文件不存在: {path}")

        size = path.stat().st_size
        media_type = _media_type(path)
        if request.method == "HEAD":  # 探测文件大小/可 Range，不吐字节
            return Response(
                status_code=200,
                media_type=media_type,
                headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
            )
        kind, spec = parse_range(request.headers.get("range"), size)

        if kind == "unsatisfiable":
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        if kind == "partial" and spec is not None:
            return StreamingResponse(
                _iter_file(path, spec.start, spec.length),
                status_code=206,
                media_type=media_type,
                headers={
                    "Content-Range": f"bytes {spec.start}-{spec.end}/{size}",
                    "Content-Length": str(spec.length),
                    "Accept-Ranges": "bytes",
                },
            )
        return StreamingResponse(
            _iter_file(path, 0, size),
            status_code=200,
            media_type=media_type,
            headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
        )

    # ---- GET /segments?content_id= ---------------------------------------

    @app.get("/segments")
    def segments(content_id: int = Query(..., ge=1)) -> dict:
        conn = db.conn()
        content = conn.execute(
            "SELECT id, title, season_ep FROM Content WHERE id = ?", (content_id,)
        ).fetchone()
        if content is None:
            raise HTTPException(status_code=404, detail=f"content {content_id} 不存在")
        rows = conn.execute(
            "SELECT id, idx, t_start, t_end, text_en, tokens_json, word_boxes_json "
            "FROM Segment WHERE content_id = ? ORDER BY idx",
            (content_id,),
        ).fetchall()
        return {
            "content_id": content_id,
            "title": content["title"],
            "season_ep": content["season_ep"],
            "segments": [
                {
                    "id": r["id"],
                    "idx": r["idx"],
                    "t_start": r["t_start"],
                    "t_end": r["t_end"],
                    "text_en": r["text_en"],
                    "tokens": _loads(r["tokens_json"], []),
                    "word_boxes": _loads(r["word_boxes_json"], None),
                }
                for r in rows
            ],
        }

    # ---- GET /lookup?surface=&segment_id= --------------------------------

    @app.get("/lookup")
    def lookup(
        surface: str = Query(..., min_length=1),
        segment_id: int | None = Query(None, ge=1),
    ) -> dict:
        conn = db.conn()
        norm = _clean_surface(surface)
        if not norm:
            raise HTTPException(status_code=400, detail="surface 为空")

        sentence = None
        if segment_id is not None:
            seg = conn.execute(
                "SELECT text_en FROM Segment WHERE id = ?", (segment_id,)
            ).fetchone()
            if seg is None:
                raise HTTPException(
                    status_code=404, detail=f"segment {segment_id} 不存在"
                )
            sentence = seg["text_en"]

        lemma, lexeme = _resolve_surface(conn, norm)
        fields, in_dict = fill_from_ecdict(conn, ecdict, lexeme, lemma, norm)
        lexeme_id = int(lexeme["id"]) if lexeme is not None else None
        return {
            "surface": norm,
            "lemma": lemma,
            "lexeme_id": lexeme_id,
            "pos": fields["pos"],
            "ipa": fields["ipa"],
            "dict_gloss": fields["dict_gloss"],
            "collected": _is_collected(conn, lexeme_id),
            "in_dict": in_dict,
            "segment_id": segment_id,
            "sentence": sentence,
        }

    # ---- POST /collect ----------------------------------------------------

    @app.post("/collect")
    def collect(payload: CollectIn = Body(...)) -> dict:
        conn = db.conn()
        norm = _clean_surface(payload.surface)
        if not norm:
            raise HTTPException(status_code=400, detail="surface 为空")
        seg = conn.execute(
            "SELECT id, text_en FROM Segment WHERE id = ?", (payload.segment_id,)
        ).fetchone()
        if seg is None:
            raise HTTPException(
                status_code=404, detail=f"segment {payload.segment_id} 不存在"
            )

        lemma, lexeme = _resolve_surface(conn, norm)
        now = _now()
        with conn:
            if lexeme is None:
                cur = conn.execute("INSERT INTO Lexeme (lemma) VALUES (?)", (lemma,))
                lexeme_id = int(cur.lastrowid)
            else:
                lexeme_id = int(lexeme["id"])
            # 点击的形式没进过 ingest（前端手动输入/OCR 变体）时补一条映射
            conn.execute(
                "INSERT INTO WordForm (surface, lexeme_id) VALUES (?, ?) "
                "ON CONFLICT (surface) DO NOTHING",
                (norm, lexeme_id),
            )

            entry = conn.execute(
                "SELECT id FROM VocabEntry WHERE lexeme_id = ?", (lexeme_id,)
            ).fetchone()
            if entry is None:
                cur = conn.execute(
                    "INSERT INTO VocabEntry (lexeme_id, added_at, note) VALUES (?,?,?)",
                    (lexeme_id, now, payload.note),
                )
                vocab_entry_id = int(cur.lastrowid)
                created = True
            else:
                vocab_entry_id = int(entry["id"])
                created = False
                if payload.note:
                    conn.execute(
                        "UPDATE VocabEntry SET note = ? WHERE id = ?",
                        (payload.note, vocab_entry_id),
                    )

            # 幂等口径：重复收藏只加 Encounter
            cur = conn.execute(
                "INSERT INTO Encounter (vocab_entry_id, segment_id, surface, added_at) "
                "VALUES (?,?,?,?)",
                (vocab_entry_id, payload.segment_id, norm, now),
            )
            encounter_id = int(cur.lastrowid)

            # 已有未完成（或已完成）的 job 不重复建；failed 允许重排
            job = conn.execute(
                "SELECT id, status FROM AnnotationJob WHERE lexeme_id = ? "
                "AND status IN ('queued','running','done') ORDER BY id DESC LIMIT 1",
                (lexeme_id,),
            ).fetchone()
            if job is None:
                cur = conn.execute(
                    "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at) "
                    "VALUES (?, 'queued', ?, ?)",
                    (lexeme_id, COLLECT_JOB_PRIORITY, now),
                )
                job_id, job_created = int(cur.lastrowid), True
            else:
                job_id, job_created = int(job["id"]), False
                # 收藏永远插队（预热任务是低优先级，见 DESIGN §5）
                conn.execute(
                    "UPDATE AnnotationJob SET priority = ? WHERE id = ? AND priority < ?",
                    (COLLECT_JOB_PRIORITY, job_id, COLLECT_JOB_PRIORITY),
                )

        # 收藏时顺手把词典字段补上，生词本立刻能显示释义
        lexeme_row = _lexeme_row(conn, lexeme_id)
        fields, in_dict = fill_from_ecdict(conn, ecdict, lexeme_row, lemma, norm)
        n_enc = conn.execute(
            "SELECT COUNT(*) c FROM Encounter WHERE vocab_entry_id = ?",
            (vocab_entry_id,),
        ).fetchone()["c"]

        return {
            "surface": norm,
            "lemma": lemma,
            "lexeme_id": lexeme_id,
            "vocab_entry_id": vocab_entry_id,
            "encounter_id": encounter_id,
            "created": created,
            "encounters": int(n_enc),
            "job_id": job_id,
            "job_created": job_created,
            "collected": True,
            "in_dict": in_dict,
            "pos": fields["pos"],
            "ipa": fields["ipa"],
            "dict_gloss": fields["dict_gloss"],
        }

    # ---- GET /vocab -------------------------------------------------------

    @app.get("/vocab")
    def vocab() -> dict:
        conn = db.conn()
        entries = conn.execute(
            "SELECT V.id, V.lexeme_id, V.added_at, V.note,"
            "       L.lemma, L.pos, L.ipa, L.dict_gloss "
            "FROM VocabEntry V JOIN Lexeme L ON L.id = V.lexeme_id "
            "ORDER BY V.added_at DESC, V.id DESC"
        ).fetchall()
        if not entries:
            return {"count": 0, "vocab": []}

        ids = [int(e["id"]) for e in entries]
        marks = ",".join("?" * len(ids))
        enc_rows = conn.execute(
            "SELECT E.id, E.vocab_entry_id, E.surface, E.added_at, E.segment_id,"
            "       S.text_en, S.t_start, S.content_id, C.title, C.season_ep "
            "FROM Encounter E "
            "LEFT JOIN Segment S ON S.id = E.segment_id "
            "LEFT JOIN Content C ON C.id = S.content_id "
            f"WHERE E.vocab_entry_id IN ({marks}) ORDER BY E.id",
            ids,
        ).fetchall()
        by_entry: dict[int, list[dict]] = {i: [] for i in ids}
        for r in enc_rows:
            by_entry[int(r["vocab_entry_id"])].append(
                {
                    "id": r["id"],
                    "surface": r["surface"],
                    "added_at": r["added_at"],
                    "segment_id": r["segment_id"],
                    "sentence": r["text_en"],
                    "t_start": r["t_start"],
                    "content_id": r["content_id"],
                    "title": r["title"],
                    "season_ep": r["season_ep"],
                }
            )

        lex_ids = [int(e["lexeme_id"]) for e in entries]
        lmarks = ",".join("?" * len(lex_ids))
        job_status = {
            int(r["lexeme_id"]): r["status"]
            for r in conn.execute(
                "SELECT lexeme_id, status FROM AnnotationJob "
                f"WHERE lexeme_id IN ({lmarks}) ORDER BY id",
                lex_ids,
            ).fetchall()
        }
        has_mnemonic = {
            int(r["lexeme_id"])
            for r in conn.execute(
                f"SELECT DISTINCT lexeme_id FROM Mnemonic WHERE lexeme_id IN ({lmarks})",
                lex_ids,
            ).fetchall()
        }

        out = []
        for e in entries:
            lid = int(e["lexeme_id"])
            encs = by_entry[int(e["id"])]
            out.append(
                {
                    "id": e["id"],
                    "lexeme_id": lid,
                    "lemma": e["lemma"],
                    "pos": e["pos"],
                    "ipa": e["ipa"],
                    "dict_gloss": e["dict_gloss"],
                    "added_at": e["added_at"],
                    "note": e["note"],
                    "mnemonic_status": job_status.get(lid, "none"),
                    "has_mnemonic": lid in has_mnemonic,
                    "encounter_count": len(encs),
                    "encounters": encs,
                }
            )
        return {"count": len(out), "vocab": out}

    # ---- GET /mnemonic?lexeme_id= ----------------------------------------

    @app.get("/mnemonic")
    def mnemonic(lexeme_id: int = Query(..., ge=1)) -> dict:
        conn = db.conn()
        lex = _lexeme_row(conn, lexeme_id)
        if lex is None:
            raise HTTPException(status_code=404, detail=f"lexeme {lexeme_id} 不存在")
        rows = conn.execute(
            "SELECT id, kind, payload_json, provider, version, edited_by_user "
            "FROM Mnemonic WHERE lexeme_id = ? ORDER BY kind, version DESC",
            (lexeme_id,),
        ).fetchall()
        # 每个 kind 只吐最新版本
        latest: dict[str, dict] = {}
        for r in rows:
            if r["kind"] in latest:
                continue
            latest[r["kind"]] = {
                "id": r["id"],
                "kind": r["kind"],
                "payload": _loads(r["payload_json"], None),
                "provider": r["provider"],
                "version": r["version"],
                "edited_by_user": bool(r["edited_by_user"]),
            }
        job = conn.execute(
            "SELECT id, status, priority, created_at, done_at FROM AnnotationJob "
            "WHERE lexeme_id = ? ORDER BY id DESC LIMIT 1",
            (lexeme_id,),
        ).fetchone()
        status = "done" if latest else (job["status"] if job is not None else "none")
        return {
            "lexeme_id": lexeme_id,
            "lemma": lex["lemma"],
            "status": status,
            "mnemonics": list(latest.values()),
            "job": dict(job) if job is not None else None,
        }

    # ---- 复习闭环（M1，规则见 app/review.py） -----------------------------

    @app.get("/review/next")
    def review_next(
        limit: int = Query(review_rules.DEFAULT_LIMIT, ge=1, le=review_rules.MAX_LIMIT),
    ) -> dict:
        """今日待复习卡（含 remaining：整个队列还剩多少，不受 limit 影响）。"""
        return review_rules.next_cards(db.conn(), limit=limit)

    @app.post("/review/answer")
    def review_answer(payload: ReviewAnswerIn = Body(...)) -> dict:
        """记一次「会 / 不会」。当天重复提交同一答案幂等（duplicate=true）。"""
        conn = db.conn()
        try:
            return review_rules.answer(
                conn, payload.vocab_entry_id, payload.result
            )
        except review_rules.BadResult as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except review_rules.UnknownEntry as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/review/stats")
    def review_stats() -> dict:
        """今日已复习 / 待复习 / 毕业总数（UTC 日历日）。"""
        return review_rules.stats(db.conn())

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(
        prog="python -m app.server", description="本地看剧学词服务（DESIGN §3）"
    )
    ap.add_argument("--db", default=os.environ.get("POI_DB", DEFAULT_DB))
    ap.add_argument("--ecdict", default=os.environ.get("POI_ECDICT", DEFAULT_ECDICT))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    application = create_app(db_path=args.db, ecdict_path=args.ecdict)
    ecdict_note = "" if Path(args.ecdict).exists() else "  (缺失 → in_dict 恒为 false)"
    print(f"[server] db={args.db}  ecdict={args.ecdict}{ecdict_note}")
    print(f"[server] http://{args.host}:{args.port}/static/player.html")
    uvicorn.run(application, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
