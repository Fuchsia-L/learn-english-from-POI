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
- 剧集导入（工单 12）：ffprobe 校验 / ffmpeg 合并 / 原子 ingest 住在
  **app/library.py**，本模块只负责收 multipart（逐块写盘）、起后台线程、
  吐作业状态 —— POST /import、GET /import/{job_id}。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import library as lib
from app import review as review_rules
from app.consts import (
    COLLECT_JOB_PRIORITY,
    DEFAULT_DB,
    DEFAULT_ECDICT,
    LIBRARY_DIRNAME,
)
from app.db import (
    ENCOUNTER_SELECT,
    SOURCE_SEGMENT,
    SOURCE_WEB,
    Database,
    encounter_view,
)
from app.ecdict import EcdictStore, fill_from_ecdict
from app.ingest import lemmatize, normalize_surface

MEDIA_CHUNK = 64 * 1024
# 单 Range：`bytes=0-1023` / `bytes=1024-` / `bytes=-500`；多 Range 只取第一段
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?$", re.IGNORECASE)

# --- 浏览器扩展的 CORS 白名单（工单 11） ------------------------------------
# 服务只听 127.0.0.1，但「本机的任意网页」都能往它上面发请求 —— 浏览器的同源策略
# 是这里唯一的门。所以只给划词插件真正要用的两个端点开口子，且只认扩展 origin：
# 收藏/查词以外的端点（/media、/segments、/review/...）一律不放，
# 随便哪个网页的 JS 都读不走生词本。
_EXT_ORIGIN_RE = re.compile(r"^(chrome|moz)-extension://[A-Za-z0-9._{}-]+/?$")
CORS_PATHS = frozenset({"/lookup", "/collect/web"})
CORS_MAX_AGE = b"600"


def is_extension_origin(origin: str | None) -> bool:
    """Origin 是不是浏览器扩展（chrome-extension:// / moz-extension://）。"""
    return bool(origin) and _EXT_ORIGIN_RE.match(origin or "") is not None


class ExtensionCORS:
    """按 (path, origin) 双条件放行的 CORS 中间件。

    不用 starlette 的 CORSMiddleware：那玩意是全局的，一开就等于给**所有**端点
    发通行证；本机上任何网页的 JS 都能把生词本读走。这里只认两个端点 + 扩展
    origin，其余请求连 Vary 都不加。

    写成裸 ASGI 类而不是 `@app.middleware("http")`：后者是 BaseHTTPMiddleware，
    每个响应都要多包一层任务，视频 Range 流也得从它身上过 —— 明明只关心两个
    端点，没道理让 /media 陪跑。这里不相干的 scope 直接原样透传。
    """

    ALLOW_METHODS = b"GET, POST, OPTIONS"
    ALLOW_HEADERS = b"Content-Type"

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _header(scope: dict, name: bytes) -> str | None:
        for k, v in scope.get("headers", []):
            if k == name:
                return v.decode("latin-1")
        return None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") not in CORS_PATHS:
            await self.app(scope, receive, send)
            return
        origin = self._header(scope, b"origin")
        if not is_extension_origin(origin):
            await self.app(scope, receive, send)
            return
        allow_origin = (origin or "").encode("latin-1")

        # 预检：只有放行组合才自己应答，其余（含无关端点/无关 origin）交给路由
        if scope.get("method") == "OPTIONS" and self._header(
            scope, b"access-control-request-method"
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", allow_origin),
                        (b"access-control-allow-methods", self.ALLOW_METHODS),
                        (b"access-control-allow-headers", self.ALLOW_HEADERS),
                        (b"access-control-max-age", CORS_MAX_AGE),
                        (b"vary", b"Origin"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"access-control-allow-origin", allow_origin),
                    (b"vary", b"Origin"),
                ]
            await send(message)

        await self.app(scope, receive, send_with_cors)


# --- 写端点的跨站防护（工单 17-1） ------------------------------------------
# CORS 挡的是**读**，不是**写**：multipart/form-data 是所谓「简单请求」，浏览器
# 不预检、直接发出去，只是不把响应交给发起页面的 JS。也就是说随便哪个网站的
# 一段 <form> 或 fetch 都能往 127.0.0.1:8000/import 塞一份 multipart，服务照收、
# 照落盘、照起后台线程 —— 攻击者读不到回包，但库和磁盘已经被写了。
# 「没给 CORS 响应头」因此不能当写入保护用。写端点必须自己认 Origin：
#   - 没有 Origin 头：本机 CLI（curl / 脚本 / requests）。浏览器发的跨站请求一定
#     带 Origin，所以放行不会给网页开口子。
#   - Origin 是本机页面（http(s)://localhost | 127.0.0.1 | [::1] [:port]）：放行，
#     播放器自己的「内容库」界面走的就是这条。
#   - 其余一律 403：外站 origin、以及 "null"（sandbox iframe / file:// / 重定向后
#     的不透明 origin —— 恰恰是攻击者最容易搞出来的那个值）。
GUARDED_WRITE_PATHS = frozenset({"/import"})
# 只有会改状态的方法要过闸：GET /import（看最近几次导入）不写任何东西。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 本机页面的 origin：三个回环主机名 + 可选端口，别的一律不认（127.0.0.2 之类
# 也不认 —— 播放器就住在这三个名字上，放宽只会多一片攻击面）。
_LOCAL_ORIGIN_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$", re.IGNORECASE
)


def is_local_origin(origin: str | None) -> bool:
    """Origin 是不是本机页面（localhost / 127.0.0.1 / [::1]，端口不限）。"""
    return bool(origin) and _LOCAL_ORIGIN_RE.match(origin or "") is not None


class LocalWriteGuard:
    """跨站写入拦截：POST /import 只认本机 Origin，且在**读请求体之前**就拒。

    写成裸 ASGI 中间件而不是 FastAPI 依赖 / 路由里的检查：Form(...) / File(...)
    这些参数是靠 multipart 解析器填的，解析发生在依赖和路由函数之前 —— 等代码
    跑到路由体里，几个 G 的上传早就读完落盘了。只有在 ASGI 层挡，才谈得上
    「一个字节都没读」：这里直接 send 403，从不 await receive()。
    """

    DENY_DETAIL = (
        "拒绝跨站导入：这个端点只接受本机页面（localhost/127.0.0.1）或本机命令行的请求"
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _header(scope: dict, name: bytes) -> str | None:
        for k, v in scope.get("headers", []):
            if k == name:
                return v.decode("latin-1")
        return None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") in GUARDED_WRITE_PATHS
            and str(scope.get("method", "")).upper() not in SAFE_METHODS
        ):
            origin = self._header(scope, b"origin")
            # 没有 Origin = 本机 CLI，放行；有 Origin 就必须是本机页面
            if origin is not None and not is_local_origin(origin):
                body = json.dumps(
                    {"detail": self.DENY_DETAIL}, ensure_ascii=False
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("latin-1")),
                            (b"vary", b"Origin"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


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


def _entry_stats(conn: sqlite3.Connection, lexeme_id: int | None) -> tuple[bool, int]:
    """(是否已收藏, 相遇次数)。没收藏就是 (False, 0)。

    次数是给查询卡显示「✓ 已收 · N 次相遇」用的（工单 11 划词插件）：
    收藏前后都要能显示同一句话，不然刚打开的卡和刚收完的卡对不上。
    """
    if lexeme_id is None:
        return False, 0
    row = conn.execute(
        "SELECT V.id, COUNT(E.id) AS n FROM VocabEntry V "
        "LEFT JOIN Encounter E ON E.vocab_entry_id = V.id "
        "WHERE V.lexeme_id = ? GROUP BY V.id",
        (lexeme_id,),
    ).fetchone()
    if row is None:
        return False, 0
    return True, int(row["n"])


def _is_collected(conn: sqlite3.Connection, lexeme_id: int | None) -> bool:
    return _entry_stats(conn, lexeme_id)[0]


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


class CollectWebIn(BaseModel):
    """POST /collect/web：浏览器划词插件的收藏（工单 11）。

    除 surface 外全可空 —— 插件在再刁钻的页面上也能退化成"光收词"，
    不因为句子没截到 / 页面没标题就收藏失败。
    """

    surface: str = Field(..., min_length=1)
    sentence: str | None = None
    url: str | None = None
    title: str | None = None
    note: str | None = None


class ReviewAnswerIn(BaseModel):
    """POST /review/answer 的请求体。result 的合法值校验放在 app/review.py
    （规则归规则层），这里只要求非空字符串 —— 非法值回 400 而不是 422。"""

    vocab_entry_id: int = Field(..., ge=1)
    result: str = Field(..., min_length=1)


# --- 应用 ------------------------------------------------------------------


def create_app(
    db_path: str | Path | None = None,
    ecdict_path: str | Path | None = None,
    library_path: str | Path | None = None,
) -> FastAPI:
    """建 app。参数优先，其次环境变量 POI_DB / POI_ECDICT / POI_LIBRARY，最后默认值。"""
    db_file = Path(db_path or os.environ.get("POI_DB") or DEFAULT_DB)
    ecdict_file = Path(ecdict_path or os.environ.get("POI_ECDICT") or DEFAULT_ECDICT)
    # 导入的媒体落在 <poi.db 所在目录>/library/<uuid>/（默认 data/library/，
    # data/ 整个在 .gitignore 里，版权素材不会进仓库）
    library_dir = Path(
        library_path
        or os.environ.get("POI_LIBRARY")
        or lib.library_root(db_file, LIBRARY_DIRNAME)
    )

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
    app.state.library_path = library_dir
    app.state.imports = lib.ImportRegistry()

    # CORS：只给划词插件的两个端点开口子（工单 11，实现见 ExtensionCORS）
    app.add_middleware(ExtensionCORS)
    # 跨站写入拦截：POST /import 只认本机 Origin（工单 17-1，实现见 LocalWriteGuard）。
    # 后加 = 更外层，所以它在 CORS 之前跑：跨站的导入请求连 multipart 解析器都碰不到。
    app.add_middleware(LocalWriteGuard)

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
        """选集下拉 + 「内容库」界面共用的一份清单（工单 12）。

        除了播放必须的字段，还带上内容库要显示的：有没有词框、媒体在不在、
        有没有音轨。音轨信息来自导入时写的 meta.json sidecar —— 列表页不能
        每次都去 ffprobe 一遍全部剧集（几十集就是几十次进程启动）。
        """
        conn = db.conn()
        rows = conn.execute(
            "SELECT C.id, C.title, C.season_ep, C.video_path, C.srt_path,"
            "       COUNT(S.id) AS n_segments, COALESCE(MAX(S.t_end), 0) AS duration,"
            "       SUM(CASE WHEN S.word_boxes_json IS NOT NULL THEN 1 ELSE 0 END)"
            "         AS n_boxes "
            "FROM Content C LEFT JOIN Segment S ON S.content_id = C.id "
            "GROUP BY C.id ORDER BY C.title, C.season_ep"
        ).fetchall()
        out = []
        for r in rows:
            video_path = Path(r["video_path"]) if r["video_path"] else None
            size = 0
            try:  # 文件可能刚被删/挂载点没了：列表页不该因此 500
                size = video_path.stat().st_size if video_path else 0
            except OSError:
                size = 0
            exists = bool(video_path) and video_path.is_file()
            meta = lib.read_meta(video_path.parent) if video_path else None
            if meta is not None and meta.get("content_id") not in (None, r["id"]):
                meta = None  # 目录被复用/搬走过，元数据对不上就当没有
            out.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "season_ep": r["season_ep"],
                    "segments": r["n_segments"],
                    "duration": round(float(r["duration"]), 3),
                    "has_video": exists,
                    "media_url": f"/media/{r['id']}",
                    "boxes_segments": int(r["n_boxes"] or 0),
                    "has_boxes": int(r["n_boxes"] or 0) > 0,
                    "media_name": video_path.name if video_path else None,
                    "media_missing": bool(video_path) and not exists,
                    "media_size": size if exists else 0,
                    # True/False 来自导入登记；老数据（CLI ingest 的）没登记 → null
                    "has_audio": (meta or {}).get("has_audio"),
                    "imported_at": (meta or {}).get("imported_at"),
                    "warnings": list((meta or {}).get("warnings") or []),
                }
            )
        return {"episodes": out}

    # ---- POST /import  （剧集导入，工单 12） --------------------------------

    @app.post("/import", status_code=202)
    async def import_episode(
        title: str = Form(...),
        season_ep: str = Form(...),
        video: UploadFile = File(...),
        srt: UploadFile = File(...),
        audio: UploadFile | None = File(None),
        boxes: UploadFile | None = File(None),
    ) -> dict:
        """收上传 → 起后台线程跑流水线，立刻返回 job_id（前端轮询 /import/{id}）。

        本函数只干两件事：把上传**逐块**落到 data/library/<uuid>/，以及挡掉
        重复导入。校验/合并/入库全在 app/library.py 的后台线程里，因为 ffmpeg
        合并一集是分钟级的，绝不能占着 HTTP 连接不放（浏览器早超时了）。
        """
        title = (title or "").strip()
        season_ep = (season_ep or "").strip()
        if not title or not season_ep:
            raise HTTPException(status_code=400, detail="剧名和季/集编号都不能为空")

        conn = db.conn()
        dup = lib.content_exists(conn, title, season_ep)
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"《{title}》{season_ep} 已经导入过（content_id={dup}）；"
                "换个季/集编号，或先把旧的删掉",
            )

        def _optional(u: UploadFile | None) -> UploadFile | None:
            # 空的 <input type=file> 也会发一个 filename="" 的分片，别当真
            return u if (u is not None and u.filename) else None

        audio = _optional(audio)
        boxes = _optional(boxes)
        if _optional(video) is None or _optional(srt) is None:
            raise HTTPException(status_code=400, detail="视频文件和 SRT 都是必填")

        library_dir.mkdir(parents=True, exist_ok=True)
        work_dir = lib.new_work_dir(library_dir)
        try:
            paths: dict[str, Path] = {}
            for key, upload, fallback in (
                ("video", video, "video.mp4"),
                ("audio", audio, "audio.m4a"),
                ("srt", srt, "subtitle.srt"),
                ("boxes", boxes, "boxes.json"),
            ):
                if upload is None:
                    continue
                dest = work_dir / lib.safe_name(upload.filename, fallback)
                if dest.exists():  # 视频和音频重名（同名不同目录）时错开
                    dest = dest.with_name(f"{key}_{dest.name}")
                size = await lib.save_upload(upload, dest)
                if size == 0:
                    raise HTTPException(
                        status_code=400, detail=f"{upload.filename} 是空文件"
                    )
                paths[key] = dest
        except HTTPException:
            lib.cleanup(work_dir)
            raise
        except OSError as exc:
            lib.cleanup(work_dir)
            raise HTTPException(status_code=500, detail=f"写入失败：{exc}") from exc

        job = app.state.imports.create(title, season_ep, work_dir)
        thread = threading.Thread(
            target=lib.run_import,
            args=(job,),
            kwargs={
                "conn_factory": db.conn,
                "db_path": db_file,
                "video_path": paths["video"],
                "srt_path": paths["srt"],
                "audio_path": paths.get("audio"),
                "boxes_path": paths.get("boxes"),
            },
            name=f"import-{job.id[:8]}",
            daemon=True,
        )
        thread.start()
        return job.as_dict()

    # ---- GET /import/{job_id} ---------------------------------------------

    @app.get("/import/{job_id}")
    def import_status(job_id: str) -> dict:
        job = app.state.imports.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"导入作业 {job_id} 不存在")
        return job.as_dict()

    # ---- GET /import  （最近几次导入，调试/刷新用） ------------------------

    @app.get("/import")
    def import_list() -> dict:
        jobs = [j.as_dict() for j in app.state.imports.recent()]
        return {"count": len(jobs), "jobs": jobs}

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
        collected, n_enc = _entry_stats(conn, lexeme_id)
        return {
            "surface": norm,
            "lemma": lemma,
            "lexeme_id": lexeme_id,
            "pos": fields["pos"],
            "ipa": fields["ipa"],
            "dict_gloss": fields["dict_gloss"],
            "collected": collected,
            "encounters": n_enc,
            "in_dict": in_dict,
            "segment_id": segment_id,
            "sentence": sentence,
        }

    # ---- 收藏（/collect 与 /collect/web 共用的一条链路） -------------------

    def _collect_core(
        norm: str,
        note: str | None,
        *,
        segment_id: int | None,
        source_kind: str,
        context: dict | None = None,
    ) -> dict:
        """Lexeme(缺则建) + WordForm + VocabEntry + Encounter + 高优先 job。

        两种来源唯一的差别只在 Encounter 那一行（segment_id / source_kind /
        context_json）；词元归一、幂等口径、入队策略完全共用，不许分叉。
        """
        conn = db.conn()
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
                    (lexeme_id, now, note),
                )
                vocab_entry_id = int(cur.lastrowid)
                created = True
            else:
                vocab_entry_id = int(entry["id"])
                created = False
                if note:
                    conn.execute(
                        "UPDATE VocabEntry SET note = ? WHERE id = ?",
                        (note, vocab_entry_id),
                    )

            # 幂等口径：重复收藏只加 Encounter
            cur = conn.execute(
                "INSERT INTO Encounter "
                "(vocab_entry_id, segment_id, surface, added_at, source_kind, context_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    vocab_entry_id,
                    segment_id,
                    norm,
                    now,
                    source_kind,
                    json.dumps(context, ensure_ascii=False) if context else None,
                ),
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

    # ---- POST /collect ----------------------------------------------------

    @app.post("/collect")
    def collect(payload: CollectIn = Body(...)) -> dict:
        conn = db.conn()
        norm = _clean_surface(payload.surface)
        if not norm:
            raise HTTPException(status_code=400, detail="surface 为空")
        seg = conn.execute(
            "SELECT id FROM Segment WHERE id = ?", (payload.segment_id,)
        ).fetchone()
        if seg is None:
            raise HTTPException(
                status_code=404, detail=f"segment {payload.segment_id} 不存在"
            )
        return _collect_core(
            norm,
            payload.note,
            segment_id=payload.segment_id,
            source_kind=SOURCE_SEGMENT,
        )

    # ---- POST /collect/web （浏览器划词插件，工单 11） ---------------------

    @app.post("/collect/web")
    def collect_web(payload: CollectWebIn = Body(...)) -> dict:
        """网页划词收藏：没有 segment，语境是页面上截到的整句 + 出处。

        与 /collect 同一条链路、同一套幂等口径（重复收藏只加 encounter），
        差别只有 Encounter 那一行：segment_id 为空，语境进 context_json。
        """
        norm = _clean_surface(payload.surface)
        if not norm:
            raise HTTPException(status_code=400, detail="surface 为空")
        context = {
            "url": (payload.url or None),
            "title": (payload.title or None),
            "sentence": (payload.sentence or None),
        }
        out = _collect_core(
            norm,
            payload.note,
            segment_id=None,
            source_kind=SOURCE_WEB,
            context=context,
        )
        out["source_kind"] = SOURCE_WEB
        out.update(context)
        return out

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
            ENCOUNTER_SELECT + f"WHERE E.vocab_entry_id IN ({marks}) ORDER BY E.id",
            ids,
        ).fetchall()
        by_entry: dict[int, list[dict]] = {i: [] for i in ids}
        for r in enc_rows:
            # 两种来源同一套键名（app/db.py encounter_view）：字幕段给时间轴，
            # 网页给 url/标题；前端按 source_kind 决定画不画「去这句」
            by_entry[int(r["vocab_entry_id"])].append(encounter_view(r))

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
    ap.add_argument(
        "--library",
        default=os.environ.get("POI_LIBRARY"),
        help="导入的剧集落在哪儿（默认 <db 所在目录>/library）",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    application = create_app(
        db_path=args.db, ecdict_path=args.ecdict, library_path=args.library
    )
    ecdict_note = "" if Path(args.ecdict).exists() else "  (缺失 → in_dict 恒为 false)"
    print(f"[server] db={args.db}  ecdict={args.ecdict}{ecdict_note}")
    print(f"[server] http://{args.host}:{args.port}/static/player.html")
    uvicorn.run(application, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
