"""剧集导入流水线（工单 12）：POST /import + app/library.py。

素材全部现造（ffmpeg lavfi 合成图 + 正弦音），不含任何真实剧集画面/台词/音频；
字幕复用 conftest 的自造 srt。本文件不发任何网络请求。

跑法:
    pytest tests/test_library.py            # 需要系统 ffmpeg + ffprobe
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app import library as lib  # noqa: E402
from app.consts import DURATION_REJECT_S, LIBRARY_META, UPLOAD_CHUNK  # noqa: E402
from app.server import create_app  # noqa: E402
from tests.conftest import FIXTURE_SRT  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="导入流水线需要系统 ffmpeg + ffprobe",
)

JOB_TIMEOUT = 90.0


# --- 自造素材 --------------------------------------------------------------


def make_video(
    path: Path,
    seconds: float = 2.0,
    audio: bool = True,
    vcodec: str = "libx264",
    acodec: str = "aac",
) -> Path:
    """合成测试图（testsrc2）视频，可选带一条正弦音轨。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"testsrc2=s=160x120:r=10:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p", "-t", str(seconds)]
    cmd += ["-c:a", acodec, "-shortest"] if audio else ["-an"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    assert path.stat().st_size > 0
    return path


def make_audio(path: Path, seconds: float = 2.0, codec: str = "aac") -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=330:duration={seconds}",
         "-c:a", codec, str(path)],
        check=True,
    )
    assert path.stat().st_size > 0
    return path


def srt_file(path: Path) -> Path:
    path.write_text(FIXTURE_SRT, encoding="utf-8")
    return path


def boxes_file(path: Path) -> Path:
    """第 1 段的假词框（形状同 extract_hardsub --boxes-json）。"""
    path.write_text(
        json.dumps(
            [{"idx": 1, "start": 1.0, "end": 3.5,
              "text": "The tall gardener went home early.",
              "words": [{"w": "The", "x": 100, "y": 1030, "width": 40, "height": 38}]}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    ecdict = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)
    return {
        "db": tmp_path / "poi.db",
        "ecdict": ecdict,
        "library": tmp_path / "library",
        "src": tmp_path,
    }


@pytest.fixture()
def client(env: dict):
    app = create_app(
        db_path=env["db"], ecdict_path=env["ecdict"], library_path=env["library"]
    )
    with TestClient(app) as c:
        c.env = env  # type: ignore[attr-defined]
        yield c


def post_import(
    client: TestClient,
    *,
    title: str = "Fixture Show",
    season_ep: str = "s01e01",
    video: Path | None = None,
    srt: Path | None = None,
    audio: Path | None = None,
    boxes: Path | None = None,
    origin: str | None = "http://127.0.0.1:8000",
):
    """默认带上本机页面的 Origin —— 全部导入用例都照浏览器的真实姿势走一遍
    跨站写入闸（工单 17-1 的 LocalWriteGuard），别让闸悄悄把内容库界面锁死。"""
    files = []
    for field, p, ctype in (
        ("video", video, "video/mp4"),
        ("srt", srt, "application/x-subrip"),
        ("audio", audio, "audio/mp4"),
        ("boxes", boxes, "application/json"),
    ):
        if p is not None:
            files.append((field, (p.name, p.read_bytes(), ctype)))
    return client.post(
        "/import",
        data={"title": title, "season_ep": season_ep},
        files=files,
        headers={"Origin": origin} if origin else {},
    )


def wait_job(client: TestClient, job_id: str, timeout: float = JOB_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        job = client.get(f"/import/{job_id}").json()
        if not seen or seen[-1] != job["stage"]:
            seen.append(job["stage"])
        if job["done"]:
            job["stages_seen"] = seen
            return job
        time.sleep(0.05)
    raise AssertionError(f"导入作业 {job_id} 超时未结束（最后阶段 {seen}）")


def import_ok(client: TestClient, **kw) -> dict:
    r = post_import(client, **kw)
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "done", job
    return job


def work_dirs(env: dict) -> list[Path]:
    root = env["library"]
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


# --- ffprobe 校验层（单测，不过 HTTP） --------------------------------------


def test_probe_reads_streams_and_duration(tmp_path: Path):
    info = lib.probe(make_video(tmp_path / "v.mp4", seconds=2.0))
    assert info.has_video and info.has_audio
    assert 1.5 < info.duration < 2.6
    assert info.vcodec == "h264" and info.acodec == "aac"


def test_probe_rejects_text_pretending_to_be_video(tmp_path: Path):
    fake = tmp_path / "not_a_video.mp4"
    fake.write_text("这其实是一份文本，只是后缀写成了 mp4\n" * 50, encoding="utf-8")
    with pytest.raises(lib.ImportError_) as exc:
        lib.probe(fake)
    assert "不是可识别的媒体文件" in str(exc.value)


def test_probe_rejects_empty_file(tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(lib.ImportError_):
        lib.probe(empty)


def test_check_pair_rejects_big_duration_gap(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=2.0, audio=False))
    a = lib.probe(make_audio(tmp_path / "a.m4a", seconds=2.0 + DURATION_REJECT_S + 3))
    with pytest.raises(lib.ImportError_) as exc:
        lib.check_pair(v, a)
    assert "时长对不上" in str(exc.value)


def test_check_pair_warns_on_small_gap(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=2.0, audio=False))
    a = lib.probe(make_audio(tmp_path / "a.m4a", seconds=5.0))  # 差 3s：警告区间
    warns = lib.check_pair(v, a)
    assert warns and "时长差" in warns[0]


def test_check_pair_warns_but_allows_silent_video(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=1.0, audio=False))
    warns = lib.check_pair(v, None)
    assert warns and "无音轨" in warns[0]


def test_check_pair_rejects_audio_file_without_audio_stream(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=2.0, audio=False))
    a = lib.probe(make_video(tmp_path / "a.mp4", seconds=2.0, audio=False))
    with pytest.raises(lib.ImportError_) as exc:
        lib.check_pair(v, a)
    assert "audio stream" in str(exc.value)


@pytest.mark.parametrize(
    "vcodec,acodec,suffix,copies",
    [
        ("h264", "aac", ".mp4", True),     # mp4 友好 → copy
        ("h264", "flac", ".mp4", False),   # mp4 吃不下 flac → 转 AAC
        ("vp9", "opus", ".webm", True),    # webm 友好 → copy
        ("vp8", "aac", ".webm", False),    # webm 吃不下 aac → 转 opus
    ],
)
def test_plan_merge_picks_container_and_audio_codec(vcodec, acodec, suffix, copies):
    v = lib.MediaInfo(Path("v"), True, False, 10.0, vcodec=vcodec)
    a = lib.MediaInfo(Path("a"), False, True, 10.0, acodec=acodec)
    got_suffix, args = lib.plan_merge(v, a)
    assert got_suffix == suffix
    assert (args == ["-c:a", "copy"]) is copies
    if not copies:
        assert args[1] in ("aac", "libopus")


def test_merge_copies_video_stream_without_reencoding(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=2.0, audio=False))
    a = lib.probe(make_audio(tmp_path / "a.m4a", seconds=2.0))
    seen: list[float] = []
    out = lib.merge_av(v, a, tmp_path, on_progress=seen.append)
    merged = lib.probe(out)
    assert merged.has_video and merged.has_audio
    assert merged.vcodec == v.vcodec        # 画面是 copy 的，编码没变
    assert seen and seen[-1] == 1.0         # 合并进度回调到位


def test_merge_reports_ffmpeg_failure(tmp_path: Path):
    v = lib.probe(make_video(tmp_path / "v.mp4", seconds=1.0, audio=False))
    broken = lib.MediaInfo(tmp_path / "nope.m4a", False, True, 1.0, acodec="aac")
    with pytest.raises(lib.ImportError_) as exc:
        lib.merge_av(v, broken, tmp_path)
    assert "合并音视频失败" in str(exc.value)


def test_safe_name_strips_paths_and_weird_chars():
    # 目录成分一律丢掉（上传名是外部输入，不能拿它拼路径）
    assert lib.safe_name("../../etc/passwd.mp4", "x.mp4") == "passwd.mp4"
    assert "/" not in lib.safe_name("a/b/c.mp4", "x.mp4")
    # 中文片名整段被洗掉，但后缀必须保住（否则 <video> 猜不出 MIME）
    assert lib.safe_name("C:\\剧集\\第一集.mkv", "video.mp4") == "video.mkv"
    assert lib.safe_name("", "video.mp4") == "video.mp4"
    assert lib.safe_name("ep 01 (1080p).mp4", "x.mp4") == "ep_01_1080p.mp4"
    # 没后缀的上传按 fallback 的后缀落地
    assert lib.safe_name("noext", "video.mp4") == "noext.mp4"


def test_save_upload_writes_in_chunks(tmp_path: Path):
    """上传落盘必须逐块：整读会把一集 1~3G 全塞进内存。"""

    class StubUpload:
        def __init__(self, blob: bytes) -> None:
            self.blob, self.pos, self.asked = blob, 0, []

        async def read(self, size: int = -1) -> bytes:
            self.asked.append(size)
            chunk = self.blob[self.pos : self.pos + size]
            self.pos += len(chunk)
            return chunk

    blob = b"x" * (UPLOAD_CHUNK * 2 + 7)
    up = StubUpload(blob)
    dest = tmp_path / "out.bin"
    n = asyncio.run(lib.save_upload(up, dest))
    assert n == len(blob) and dest.read_bytes() == blob
    # 每次读都是有限大小（3 块 + 1 次读到 EOF），没有一次 read(-1) 整读
    assert up.asked == [UPLOAD_CHUNK] * 4
    assert max(up.asked) == UPLOAD_CHUNK


# --- HTTP：完整导入 --------------------------------------------------------


def test_import_self_contained_video(client: TestClient, env: dict):
    """自带音轨的单文件：跳过合并，直接校验 + 入库。"""
    job = import_ok(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=2.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    assert job["warnings"] == []
    assert job["stats"]["segments"] == 5
    assert "probing" in job["stages_seen"] and "merging" not in job["stages_seen"]

    eps = client.get("/episodes").json()["episodes"]
    assert len(eps) == 1
    ep = eps[0]
    assert ep["id"] == job["content_id"]
    assert (ep["title"], ep["season_ep"]) == ("Fixture Show", "s01e01")
    assert ep["segments"] == 5 and ep["duration"] > 0
    assert ep["has_video"] is True and ep["has_audio"] is True
    assert ep["has_boxes"] is False and ep["media_missing"] is False

    # 媒体真的能按 Range 读
    r = client.get(f"/media/{ep['id']}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100

    # 落盘位置：data/library/<uuid>/，媒体 + 字幕 + 元数据
    dirs = work_dirs(env)
    assert len(dirs) == 1
    names = {p.name for p in dirs[0].iterdir()}
    assert "ep.mp4" in names and "ep.srt" in names and LIBRARY_META in names
    meta = json.loads((dirs[0] / LIBRARY_META).read_text(encoding="utf-8"))
    assert meta["has_audio"] is True and meta["merged"] is False


def test_import_from_foreign_page_is_refused_end_to_end(client: TestClient, env: dict):
    """工单 17-1：整条链路验一遍 —— 外站页面发的导入请求 403，磁盘上什么都不留。

    与 tests/test_server.py 里那组的区别：这里用的是真视频 + 真 srt，
    证明「被拒」不是因为素材不合法，而是因为 Origin 不是本机页面。
    """
    r = post_import(
        client,
        video=make_video(env["src"] / "evil.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "evil.srt"),
        origin="https://evil.example",
    )
    assert r.status_code == 403 and "跨站" in r.json()["detail"]
    assert work_dirs(env) == []
    assert client.get("/import").json()["count"] == 0
    assert client.get("/episodes").json()["episodes"] == []

    # 同一份素材、换成本机 origin：照常导入成功（拒的是来源，不是素材）
    job = import_ok(
        client,
        video=make_video(env["src"] / "ok.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "ok.srt"),
    )
    assert job["stage"] == "done"


def test_import_merges_separate_audio_and_video(client: TestClient, env: dict):
    """音视频分离：先 ffprobe 验流+时长，再合并，合并结果两条流都在。"""
    r = post_import(
        client,
        video=make_video(env["src"] / "picture.mp4", seconds=2.0, audio=False),
        audio=make_audio(env["src"] / "sound.m4a", seconds=2.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "done", job
    # 阶段顺序：校验 → 合并 →（入库，可能快到轮询没抓到）→ 完成
    assert job["stages_seen"][0] == "probing"
    assert "merging" in job["stages_seen"] and job["stages_seen"][-1] == "done"
    assert job["warnings"] == []

    row = client.get("/episodes").json()["episodes"][0]
    assert row["has_audio"] is True
    dirs = work_dirs(env)
    meta = json.loads((dirs[0] / LIBRARY_META).read_text(encoding="utf-8"))
    assert meta["merged"] is True
    media = dirs[0] / meta["media"]
    info = lib.probe(media)
    assert info.has_video and info.has_audio
    # 合并成功后原始分轨已清掉（省一半磁盘），只剩合并件 + 字幕 + 元数据
    assert not (dirs[0] / "picture.mp4").exists()
    assert not (dirs[0] / "sound.m4a").exists()

    assert client.get(f"/media/{row['id']}").status_code == 200


def test_import_rejects_duration_mismatch_and_leaves_nothing(
    client: TestClient, env: dict
):
    r = post_import(
        client,
        video=make_video(env["src"] / "picture.mp4", seconds=2.0, audio=False),
        audio=make_audio(env["src"] / "sound.m4a", seconds=2.0 + DURATION_REJECT_S + 4),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "error" and "时长对不上" in job["error"]
    assert client.get("/episodes").json()["episodes"] == []   # 没留半条 Content
    assert work_dirs(env) == []                               # uuid 目录清干净了


def test_import_rejects_text_pretending_to_be_video(client: TestClient, env: dict):
    fake = env["src"] / "fake.mp4"
    fake.write_text("not a video at all\n" * 200, encoding="utf-8")
    r = post_import(client, video=fake, srt=srt_file(env["src"] / "ep.srt"))
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "error" and "不是可识别的媒体文件" in job["error"]
    assert client.get("/episodes").json()["episodes"] == []
    assert work_dirs(env) == []


def test_import_rejects_audio_file_without_audio_stream(client: TestClient, env: dict):
    r = post_import(
        client,
        video=make_video(env["src"] / "picture.mp4", seconds=2.0, audio=False),
        audio=make_video(env["src"] / "alsopicture.mp4", seconds=2.0, audio=False),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "error" and "audio stream" in job["error"]
    assert work_dirs(env) == []


def test_import_allows_silent_video_with_warning(client: TestClient, env: dict):
    """默片素材照样能导（只是标 ⚠ 无音轨），不该被拒。"""
    job = import_ok(
        client,
        video=make_video(env["src"] / "silent.mp4", seconds=2.0, audio=False),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    assert any("无音轨" in w for w in job["warnings"])
    ep = client.get("/episodes").json()["episodes"][0]
    assert ep["has_audio"] is False
    assert any("无音轨" in w for w in ep["warnings"])


def test_import_rejects_duplicate_title_and_season_ep(client: TestClient, env: dict):
    video = make_video(env["src"] / "ep.mp4", seconds=1.0)
    srt = srt_file(env["src"] / "ep.srt")
    first = import_ok(client, video=video, srt=srt)

    r = post_import(client, video=video, srt=srt)
    assert r.status_code == 409
    assert "已经导入过" in r.json()["detail"]
    # 旧的一集原封不动，磁盘上也没多出目录
    eps = client.get("/episodes").json()["episodes"]
    assert len(eps) == 1 and eps[0]["id"] == first["content_id"]
    assert len(work_dirs(env)) == 1


def test_import_applies_boxes_json(client: TestClient, env: dict):
    job = import_ok(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=2.0),
        srt=srt_file(env["src"] / "ep.srt"),
        boxes=boxes_file(env["src"] / "ep.boxes.json"),
    )
    ep = client.get("/episodes").json()["episodes"][0]
    assert ep["has_boxes"] is True and ep["boxes_segments"] == 1
    segs = client.get("/segments", params={"content_id": job["content_id"]}).json()
    assert segs["segments"][0]["word_boxes"][0]["w"] == "The"


def test_import_rejects_broken_boxes_json(client: TestClient, env: dict):
    bad = env["src"] / "bad.boxes.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    r = post_import(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "ep.srt"),
        boxes=bad,
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "error" and "boxes.json" in job["error"]
    assert client.get("/episodes").json()["episodes"] == []
    assert work_dirs(env) == []


def test_import_rejects_empty_srt(client: TestClient, env: dict):
    empty = env["src"] / "empty.srt"
    empty.write_text("这文件里一条时间轴都没有\n", encoding="utf-8")
    r = post_import(
        client, video=make_video(env["src"] / "ep.mp4", seconds=1.0), srt=empty
    )
    job = wait_job(client, r.json()["job_id"])
    assert job["stage"] == "error" and "SRT" in job["error"]
    assert client.get("/episodes").json()["episodes"] == []
    assert work_dirs(env) == []


def test_import_requires_title_and_season_ep(client: TestClient, env: dict):
    r = post_import(
        client,
        title="   ",
        video=make_video(env["src"] / "ep.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    assert r.status_code == 400
    assert work_dirs(env) == []


def test_import_rejects_empty_upload(client: TestClient, env: dict):
    empty = env["src"] / "zero.mp4"
    empty.write_bytes(b"")
    r = post_import(client, video=empty, srt=srt_file(env["src"] / "ep.srt"))
    assert r.status_code == 400 and "空文件" in r.json()["detail"]
    assert work_dirs(env) == []


def test_import_status_404_for_unknown_job(client: TestClient):
    assert client.get("/import/deadbeef").status_code == 404


def test_import_list_reports_recent_jobs(client: TestClient, env: dict):
    job = import_ok(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    jobs = client.get("/import").json()
    assert jobs["count"] == 1 and jobs["jobs"][0]["job_id"] == job["job_id"]


def test_imported_episode_still_plays_after_restart(client: TestClient, env: dict):
    """重启后仍可播放：媒体在库外的 data/library 里，重建 app 照样 Range 读。"""
    job = import_ok(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=2.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    content_id = job["content_id"]

    fresh = create_app(
        db_path=env["db"], ecdict_path=env["ecdict"], library_path=env["library"]
    )
    with TestClient(fresh) as c2:
        eps = c2.get("/episodes").json()["episodes"]
        assert [e["id"] for e in eps] == [content_id]
        assert eps[0]["has_video"] is True
        head = c2.head(f"/media/{content_id}")
        size = int(head.headers["Content-Length"])
        assert size > 0 and head.headers["Accept-Ranges"] == "bytes"
        r = c2.get(f"/media/{content_id}", headers={"Range": f"bytes={size - 10}-"})
        assert r.status_code == 206 and len(r.content) == 10
        assert r.headers["Content-Range"] == f"bytes {size - 10}-{size - 1}/{size}"


def test_media_status_flags_missing_file(client: TestClient, env: dict):
    job = import_ok(
        client,
        video=make_video(env["src"] / "ep.mp4", seconds=1.0),
        srt=srt_file(env["src"] / "ep.srt"),
    )
    meta = json.loads((work_dirs(env)[0] / LIBRARY_META).read_text(encoding="utf-8"))
    (work_dirs(env)[0] / meta["media"]).unlink()
    ep = client.get("/episodes").json()["episodes"][0]
    assert ep["id"] == job["content_id"]
    assert ep["has_video"] is False and ep["media_missing"] is True
