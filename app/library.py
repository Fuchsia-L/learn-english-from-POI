"""剧集导入流水线（工单 12）：上传 → ffprobe 校验 → ffmpeg 合并 → 原子 ingest。

分层同 app/review.py 的路子：**规则和外部进程调用住这儿，app/server.py 只做
HTTP 壳子**（收 multipart、起线程、吐作业状态）。这样合并策略、时长阈值、
失败清理这些真正会出错的逻辑可以脱离 HTTP 单测。

流水线（run_import）:
    probing   ffprobe 两个输入：视频必须真有 video stream，音频必须真有
              audio stream；分离导入时两者时长差 >DURATION_REJECT_S 直接拒。
    merging   只在音视频分离时发生：画面永远 -c:v copy（重编码一集要几十分钟，
              而且画质白掉一轮），音频按目标容器决定 copy 还是转码。
    ingesting 复用 app.ingest.ingest_srt（同一个 conn、同一个事务）。

失败口径（验收 §5）：任何一步失败都 rmtree 掉本次 uuid 目录、不留半条 Content。
uuid 目录是这次导入新建的，所以"不覆盖已有文件"是天然成立的——
唯一能撞车的是 (title, season_ep) 重复，那个在入库前显式拒绝。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.consts import (
    DURATION_REJECT_S,
    DURATION_WARN_S,
    IMPORT_JOB_KEEP,
    LIBRARY_META,
    UPLOAD_CHUNK,
)
from app.ingest import ingest_srt, load_boxes, parse_srt_file

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"

# 合并输出容器的选择：只看视频编码，因为画面一定是 copy 的（不能重编码）。
# vp8/vp9 只能进 webm；其余（h264/hevc/av1/mpeg4…）进 mp4。
WEBM_VCODECS = frozenset({"vp8", "vp9"})
# 各容器里能直接 copy 的音频编码；不在表里的（flac/pcm/dts/truehd…）转码。
MP4_AUDIO_OK = frozenset({"aac", "mp3", "alac", "ac3", "eac3"})
WEBM_AUDIO_OK = frozenset({"opus", "vorbis"})
MP4_AUDIO_FALLBACK = ["-c:a", "aac", "-b:a", "192k"]
WEBM_AUDIO_FALLBACK = ["-c:a", "libopus", "-b:a", "128k"]

MERGED_STEM = "merged"

STAGE_QUEUED = "queued"
STAGE_PROBING = "probing"
STAGE_MERGING = "merging"
STAGE_INGESTING = "ingesting"
STAGE_DONE = "done"
STAGE_ERROR = "error"

STAGE_LABEL = {
    STAGE_QUEUED: "排队中",
    STAGE_PROBING: "校验媒体…",
    STAGE_MERGING: "合并音视频…",
    STAGE_INGESTING: "字幕入库…",
    STAGE_DONE: "导入完成",
    STAGE_ERROR: "导入失败",
}


class ImportError_(Exception):
    """导入失败（面向用户的中文原因）。名字带下划线避开内置 ImportError。"""


MediaError = ImportError_  # 对外的别名：媒体校验失败也是导入失败


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- ffprobe ---------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 读出来的、这条流水线真正关心的那几件事。"""

    path: Path
    has_video: bool
    has_audio: bool
    duration: float
    vcodec: str | None = None
    acodec: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.path.name,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "duration": round(self.duration, 3),
            "vcodec": self.vcodec,
            "acodec": self.acodec,
        }


def _run(cmd: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:  # 没装 ffmpeg/ffprobe
        raise ImportError_(
            f"找不到 {cmd[0]}：导入剧集需要系统安装 ffmpeg（含 ffprobe）"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ImportError_(f"{cmd[0]} 超时（{timeout}s）") from exc


def _as_float(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def probe(path: str | Path, timeout: float = 120.0) -> MediaInfo:
    """ffprobe 一个文件 → MediaInfo。不是媒体（文本冒充视频）就抛 ImportError_。"""
    p = Path(path)
    if not p.is_file():
        raise ImportError_(f"文件不存在: {p.name}")
    if p.stat().st_size == 0:
        raise ImportError_(f"{p.name} 是空文件")
    proc = _run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(p)],
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "未知错误"
        raise ImportError_(f"{p.name} 不是可识别的媒体文件（ffprobe: {tail}）")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise ImportError_(f"{p.name} 的 ffprobe 输出无法解析") from exc

    streams = data.get("streams") or []
    if not isinstance(streams, list):
        streams = []
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    # 封面图/缩略图不算画面：mp3 里塞的专辑封面就是一条 video stream
    vstreams = [s for s in vstreams if not (s.get("disposition") or {}).get("attached_pic")]

    duration = _as_float((data.get("format") or {}).get("duration"))
    if duration <= 0:
        duration = max(
            [_as_float(s.get("duration")) for s in streams] or [0.0]
        )
    if not streams:
        raise ImportError_(f"{p.name} 里没有任何音视频流")
    return MediaInfo(
        path=p,
        has_video=bool(vstreams),
        has_audio=bool(astreams),
        duration=duration,
        vcodec=(vstreams[0].get("codec_name") if vstreams else None),
        acodec=(astreams[0].get("codec_name") if astreams else None),
    )


# --- 合并 ------------------------------------------------------------------


def plan_merge(video: MediaInfo, audio: MediaInfo) -> tuple[str, list[str]]:
    """→ (输出后缀, 音频参数)。画面永远 copy，所以容器只看视频编码。"""
    if (video.vcodec or "").lower() in WEBM_VCODECS:
        ok, fallback, suffix = WEBM_AUDIO_OK, WEBM_AUDIO_FALLBACK, ".webm"
    else:
        ok, fallback, suffix = MP4_AUDIO_OK, MP4_AUDIO_FALLBACK, ".mp4"
    if (audio.acodec or "").lower() in ok:
        return suffix, ["-c:a", "copy"]
    return suffix, list(fallback)


_PROGRESS_RE = re.compile(r"^[a-z_0-9]+=")


def merge_av(
    video: MediaInfo,
    audio: MediaInfo,
    out_dir: str | Path,
    on_progress: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> Path:
    """把画面和音轨合成一个文件，返回输出路径。

    -c:v copy 是硬要求（重编码一集电视剧要几十分钟，画质还白掉一轮）；
    音频能 copy 就 copy，容器吃不下的编码（flac/pcm/dts…）才转成 AAC/Opus。
    """
    out = Path(out_dir)
    suffix, audio_args = plan_merge(video, audio)
    dest = out / f"{MERGED_STEM}{suffix}"
    cmd = [
        FFMPEG, "-y", "-v", "error", "-nostdin",
        "-i", str(video.path), "-i", str(audio.path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", *audio_args,
        "-shortest",
    ]
    if suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dest)]

    total = max(min(video.duration, audio.duration), 0.001)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except FileNotFoundError as exc:
        raise ImportError_(f"找不到 {FFMPEG}：导入剧集需要系统安装 ffmpeg") from exc

    noise: list[str] = []
    assert proc.stdout is not None
    # stderr 并进 stdout 单管道读：分两个管道读会在错误刷屏时把自己写死锁。
    # 进度行长得像 key=value，其余当成 ffmpeg 的报错留着报给用户。
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if _PROGRESS_RE.match(line):
            key, _, value = line.partition("=")
            if key == "out_time_us" and on_progress is not None:
                done = _as_float(value) / 1_000_000.0
                on_progress(max(0.0, min(1.0, done / total)))
            continue
        noise.append(line)
        del noise[:-20]
    proc.wait(timeout=timeout)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        tail = noise[-1] if noise else f"ffmpeg 退出码 {proc.returncode}"
        raise ImportError_(f"合并音视频失败（ffmpeg: {tail}）")
    if on_progress is not None:
        on_progress(1.0)
    return dest


def check_pair(video: MediaInfo, audio: MediaInfo | None) -> list[str]:
    """按验收 §3 校验一对输入，返回告警列表（该拒的直接抛）。"""
    warnings: list[str] = []
    if not video.has_video:
        raise ImportError_(
            f"{video.path.name} 里没有 video stream（这不是视频文件）"
        )
    if audio is None:
        if not video.has_audio:
            # 默片素材（纯 OCR 用的无声片段）仍然允许导入，只是标出来
            warnings.append("视频没有音轨，导入后只能看画面（⚠ 无音轨）")
        return warnings

    if not audio.has_audio:
        raise ImportError_(
            f"{audio.path.name} 里没有 audio stream（这不是音频文件）"
        )
    diff = abs(video.duration - audio.duration)
    if diff > DURATION_REJECT_S:
        raise ImportError_(
            f"音视频时长对不上：视频 {video.duration:.1f}s，音频 {audio.duration:.1f}s，"
            f"差 {diff:.1f}s（上限 {DURATION_REJECT_S:.0f}s）——多半拿错了片源"
        )
    if diff > DURATION_WARN_S:
        warnings.append(
            f"音视频时长差 {diff:.1f}s（超过 {DURATION_WARN_S:.0f}s），"
            "合并后可能有轻微不同步"
        )
    return warnings


# --- 作业状态（前端轮询） --------------------------------------------------


@dataclass
class ImportJob:
    """一次导入的可观测状态。只活在内存里，进程重启即丢（媒体和库都已落盘）。"""

    id: str
    title: str
    season_ep: str
    dir: Path
    stage: str = STAGE_QUEUED
    progress: float = 0.0  # 当前阶段的进度 0..1（入库阶段不可分，只有 0/1）
    message: str = STAGE_LABEL[STAGE_QUEUED]
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    content_id: int | None = None
    stats: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if "stage" in kw and "message" not in kw:
                self.message = STAGE_LABEL.get(self.stage, self.stage)
            self.updated_at = _now()

    def warn(self, msg: str) -> None:
        with self._lock:
            if msg not in self.warnings:
                self.warnings.append(msg)
            self.updated_at = _now()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "job_id": self.id,
                "title": self.title,
                "season_ep": self.season_ep,
                "stage": self.stage,
                "progress": round(float(self.progress), 4),
                "message": self.message,
                "warnings": list(self.warnings),
                "error": self.error,
                "content_id": self.content_id,
                "stats": dict(self.stats),
                "media": dict(self.media),
                "done": self.stage in (STAGE_DONE, STAGE_ERROR),
                "ok": self.stage == STAGE_DONE,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }


class ImportRegistry:
    """最近若干条导入作业。线程安全，容量有限（IMPORT_JOB_KEEP）。"""

    def __init__(self, keep: int = IMPORT_JOB_KEEP) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ImportJob] = {}
        self._order: list[str] = []
        self._keep = keep

    def create(self, title: str, season_ep: str, work_dir: Path) -> ImportJob:
        job = ImportJob(
            id=uuid.uuid4().hex, title=title, season_ep=season_ep, dir=work_dir
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._keep:
                self._jobs.pop(self._order.pop(0), None)
        return job

    def get(self, job_id: str) -> ImportJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[ImportJob]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]


# --- 流水线 ----------------------------------------------------------------


def content_exists(conn, title: str, season_ep: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM Content WHERE title = ? AND season_ep = ?", (title, season_ep)
    ).fetchone()
    return int(row["id"]) if row is not None else None


def write_meta(work_dir: Path, payload: dict) -> None:
    """把这集的媒体元数据写在 uuid 目录里（/episodes 读它显示"有无音轨"）。

    单独一个 sidecar 而不是加数据库列：它描述的是**磁盘上这个文件**，
    文件跟目录一起被删/被搬走时元数据自然跟着走，不会在库里留孤儿字段。
    """
    (work_dir / LIBRARY_META).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_meta(work_dir: str | Path) -> dict | None:
    p = Path(work_dir) / LIBRARY_META
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_import(
    job: ImportJob,
    *,
    conn_factory: Callable[[], Any],
    db_path: str | Path,
    video_path: Path,
    srt_path: Path,
    audio_path: Path | None = None,
    boxes_path: Path | None = None,
    keep_sources: bool = False,
) -> dict:
    """跑完整条流水线（在后台线程里调用）。失败清理 uuid 目录并把原因写进 job。"""
    work_dir = job.dir
    ingested = False
    try:
        # --- 1. 校验 -------------------------------------------------------
        job.set(stage=STAGE_PROBING, progress=0.0)
        vinfo = probe(video_path)
        ainfo = probe(audio_path) if audio_path is not None else None
        for msg in check_pair(vinfo, ainfo):
            job.warn(msg)
        job.set(progress=1.0, media={"video": vinfo.as_dict(),
                                     "audio": ainfo.as_dict() if ainfo else None})

        # 字幕/词框先解析一遍再动数据库：坏 srt 不该在库里留半条 Content
        cues = parse_srt_file(srt_path)
        if not cues:
            raise ImportError_("SRT 里没有解析出任何字幕段（文件是空的或格式不对）")
        if boxes_path is not None:
            try:
                load_boxes(boxes_path)
            except (ValueError, OSError) as exc:
                raise ImportError_(f"boxes.json 读不了：{exc}") from exc

        # --- 2. 合并 -------------------------------------------------------
        if ainfo is not None:
            job.set(stage=STAGE_MERGING, progress=0.0)
            media = merge_av(
                vinfo, ainfo, work_dir, on_progress=lambda p: job.set(progress=p)
            )
            merged_info = probe(media)
            if not (merged_info.has_video and merged_info.has_audio):
                raise ImportError_("合并结果里缺流（ffmpeg 没报错但输出不对）")
            if not keep_sources:
                # 合并成功后原始分轨就是死重量（一集 1~2G），删掉省一半磁盘。
                # 删的是我们自己刚落的副本，用户的原文件一直没动过。
                video_path.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            final = merged_info
            job.set(progress=1.0)
        else:
            media = video_path
            final = vinfo

        # --- 3. 入库（原子） -----------------------------------------------
        job.set(stage=STAGE_INGESTING, progress=0.0)
        conn = conn_factory()
        dup = content_exists(conn, job.title, job.season_ep)
        if dup is not None:
            raise ImportError_(
                f"《{job.title}》{job.season_ep} 已经在库里了（content_id={dup}），"
                "不覆盖已有内容"
            )
        stats = ingest_srt(
            db_path=db_path,
            srt_path=srt_path,
            title=job.title,
            season_ep=job.season_ep,
            video_path=str(media),
            conn=conn,
            boxes_path=boxes_path,
        )
        # 过了这条线就不许再删媒体了：库里已经有一条指着它的 Content，
        # 后面哪一步出岔子也只是"元数据没写全"，不是"导入失败"。
        ingested = True
        try:
            write_meta(
                work_dir,
                {
                    "job_id": job.id,
                    "title": job.title,
                    "season_ep": job.season_ep,
                    "imported_at": _now(),
                    "media": media.name,
                    "merged": ainfo is not None,
                    "has_audio": final.has_audio,
                    "duration": round(final.duration, 3),
                    "vcodec": final.vcodec,
                    "acodec": final.acodec,
                    "content_id": stats["content_id"],
                    "warnings": list(job.warnings),
                },
            )
        except OSError as exc:  # 元数据是旁路：写不出来不该回滚已经入库的一集
            job.warn(f"媒体元数据没写成（不影响播放）：{exc}")

        job.set(
            stage=STAGE_DONE,
            progress=1.0,
            content_id=int(stats["content_id"]),
            stats={k: v for k, v in stats.items() if isinstance(v, (int, str))},
            message=(
                f"完成：{stats['segments']} 段字幕"
                + (f"，词框 {stats['boxes_applied']} 段" if boxes_path else "")
            ),
        )
        return job.as_dict()
    except Exception as exc:  # noqa: BLE001 —— 任何失败都要清干净再往外报
        reason = str(exc) if isinstance(exc, ImportError_) else f"{type(exc).__name__}: {exc}"
        if not ingested:
            cleanup(work_dir)
        job.set(stage=STAGE_ERROR, error=reason, message=f"导入失败：{reason}")
        return job.as_dict()


def cleanup(work_dir: str | Path) -> None:
    """删掉本次导入的 uuid 目录（失败清理）。删不掉不抛，只是留点垃圾。"""
    shutil.rmtree(Path(work_dir), ignore_errors=True)


# --- 上传落盘 --------------------------------------------------------------

# 文件名只保留 ASCII 安全字符：上传名是外部输入，绝不能拿它拼路径
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(raw: str | None, fallback: str) -> str:
    """上传文件名 → 落盘文件名。去目录、去怪字符，保底给 fallback。

    扩展名单独保住：中文片名会被整段替换成下划线（`第一集.mkv` → `_.mkv`），
    要是连后缀一起洗掉，mimetypes 猜不出类型，<video> 就拿不到能播的 MIME。
    """
    base = Path((raw or "").replace("\\", "/")).name
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._")
    ext = _SAFE_NAME_RE.sub("", ext).lower()[:8]
    if not stem:
        stem = Path(fallback).stem
    if not ext:
        ext = Path(fallback).suffix.lstrip(".")
    return stem[-100:] + (f".{ext}" if ext else "")


async def save_upload(upload: Any, dest: str | Path, chunk_size: int = UPLOAD_CHUNK) -> int:
    """把 UploadFile 逐块写到 dest，返回字节数。

    **不许 `await upload.read()` 整读**：一集 1080p 有 1~3 个 G，整读进内存等于
    在用户机器上开一个同等大小的坑。这里按 chunk_size 一块块搬，内存占用恒定。
    """
    total = 0
    dest = Path(dest)
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def library_root(db_path: str | Path, dirname: str) -> Path:
    """媒体库根目录：<poi.db 所在目录>/library（默认 data/library）。"""
    parent = Path(db_path).resolve().parent
    return parent / dirname


def new_work_dir(root: str | Path) -> Path:
    """新建一个 uuid 目录。uuid 保证不撞已有文件（验收 §5「不覆盖」）。"""
    d = Path(root) / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=False)
    return d


def iter_media_files(work_dir: str | Path) -> Iterable[Path]:
    d = Path(work_dir)
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []
