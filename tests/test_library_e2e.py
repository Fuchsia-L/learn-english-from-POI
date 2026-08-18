"""内容库界面 + 导入表单的端到端（工单 12；配合 tests/test_library.py 的服务端用例）。

跑法:
    pytest tests/test_library_e2e.py         # 需要 ffmpeg + playwright chromium
    pytest -m "not slow"                     # 跳过本文件

版权红线（DESIGN §0 §6）：素材全部现造 —— 视频是 ffmpeg lavfi 合成测试图，
音频是正弦波，字幕是自编英文句子。不含任何真实剧集画面/台词/音轨。

编码选型：画面 VP8 + 音频 Vorbis（合并后进 webm，两条流都是 copy）。
headless chromium 不带专利解码器，H.264/AAC 在这儿播不出来。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app.ingest import ingest_srt  # noqa: E402
from app.server import create_app  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg 造夹具素材"),
]

# --- 自造素材 --------------------------------------------------------------

SRT_A = """1
00:00:00,500 --> 00:00:02,000
The quiet cop began a stakeout near the door.

2
00:00:02,200 --> 00:00:04,000
My cousin wants to hire a new driver.
"""

SRT_B = """1
00:00:00,500 --> 00:00:02,500
A late train left the empty station.
"""

IMPORT_TITLE = "Imported Show"
IMPORT_SEASON_EP = "s02e07"


def make_silent_video(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=s=240x135:r=10:d={seconds}",
         "-c:v", "libvpx", "-b:v", "400k", "-an", str(path)],
        check=True,
    )
    return path


def make_audio(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=420:duration={seconds}",
         "-c:a", "libvorbis", str(path)],
        check=True,
    )
    return path


def make_boxes(path: Path) -> Path:
    path.write_text(
        json.dumps([{
            "idx": 1, "start": 0.5, "end": 2.0,
            "text": "The quiet cop began a stakeout near the door.",
            "words": [{"w": "The", "x": 120, "y": 1032, "width": 60, "height": 40}],
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("library_e2e")

    ecdict = d / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)

    # 库里先有一集（走 CLI ingest 那条老路：媒体不在 library/ 目录里）
    seeded_video = d / "seeded.webm"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=s=240x135:r=10:d=4",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
         "-c:v", "libvpx", "-b:v", "400k", "-c:a", "libvorbis", "-shortest",
         str(seeded_video)],
        check=True,
    )
    srt = d / "seeded.srt"
    srt.write_text(SRT_A, encoding="utf-8")
    db = d / "poi.db"
    stats = ingest_srt(
        db_path=db, srt_path=srt, title="Seeded Show", season_ep="s01e01",
        video_path=str(seeded_video), boxes_path=make_boxes(d / "seeded.boxes.json"),
    )
    assert stats["segments"] == 2 and stats["boxes_applied"] == 1

    # 待导入的素材：分离的画面 + 音轨（走合并那条路）
    up = d / "uploads"
    up.mkdir()
    return {
        "dir": d,
        "db": db,
        "ecdict": ecdict,
        "library": d / "library",
        "seeded_id": stats["content_id"],
        "video": make_silent_video(up / "picture.webm"),
        "audio": make_audio(up / "sound.ogg"),
        "srt": (up / "episode.srt", (up / "episode.srt").write_text(SRT_B, encoding="utf-8"))[0],
        "uploads": up,
    }


@pytest.fixture(scope="module")
def server_url(workspace: dict):
    import uvicorn

    port = free_port()
    app = create_app(
        db_path=workspace["db"],
        ecdict_path=workspace["ecdict"],
        library_path=workspace["library"],
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def player(page, server_url: str):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    # 本文件专门要验"被拒绝"的路径（409 重复导入等），chromium 会把非 2xx 响应
    # 记成 console error。那是网络状态噪音、不是 JS 报错，按前缀过掉；
    # pageerror（真异常）一条都不放。
    page.on(
        "console",
        lambda m: errors.append("console: " + m.text)
        if (m.type == "error" and not m.text.startswith("Failed to load resource"))
        else None,
    )
    page.goto(server_url + "/static/player.html")
    page.wait_for_selector("#ep option[value]", state="attached")
    yield page
    assert errors == [], f"页面有 JS 报错: {errors}"


def set_view(page, view: str) -> None:
    page.click(f"#tabs button[data-view='{view}']")
    page.wait_for_selector(f"#view-{view}.on")


def fill_import_form(page, ws: dict, title: str, season_ep: str, *, audio=True, video=None):
    page.fill("#impTitle", title)
    page.fill("#impSeasonEp", season_ep)
    page.set_input_files("#impVideo", str(video or ws["video"]))
    page.set_input_files("#impSrt", str(ws["srt"]))
    if audio:
        page.set_input_files("#impAudio", str(ws["audio"]))


# --- 用例：内容库界面 ------------------------------------------------------


def test_library_tab_sits_next_to_play_and_vocab(player):
    tabs = player.eval_on_selector_all("#tabs button", "bs => bs.map(b => b.textContent)")
    assert tabs == ["[ 播放 ]", "[ 内容库 ]", "[ 生词本 ]"]
    set_view(player, "lib")
    assert player.locator("#view-lib").is_visible()
    assert player.locator("#view-play").is_hidden()
    assert player.locator("#view-vocab").is_hidden()
    # 内容库是全屏界面：播放界面的顶栏控件收起
    assert player.locator("#modes").is_hidden() and player.locator("#ep").is_hidden()


def test_library_lists_every_ingested_episode(player, workspace: dict):
    set_view(player, "lib")
    player.wait_for_selector("#liblist .lrow[data-content-id]")
    row = player.locator(f"#liblist .lrow[data-content-id='{workspace['seeded_id']}']")
    assert row.count() == 1
    text = row.inner_text()
    assert "Seeded Show" in text and "s01e01" in text
    assert "2 段" in text                       # 段数
    assert "00:04" in text                      # 时长（末段 t_end）
    assert "✓ 1 段" in text                     # 有词框
    assert "✓" in row.locator(".ms").inner_text()  # 媒体在
    assert player.locator("#libcount").inner_text() == "1 集"


def test_clicking_a_row_opens_that_episode_in_player(player, workspace: dict):
    set_view(player, "lib")
    player.click(f"#liblist .lrow[data-content-id='{workspace['seeded_id']}']")
    player.wait_for_selector("#view-play.on")
    assert player.evaluate("document.body.dataset.view") == "play"
    assert player.input_value("#ep") == str(workspace["seeded_id"])
    player.wait_for_function("() => document.getElementById('video').readyState >= 1")
    assert player.evaluate("document.getElementById('video').src").endswith(
        f"/media/{workspace['seeded_id']}"
    )


# --- 用例：导入 ------------------------------------------------------------


def test_import_merges_uploads_and_switches_to_new_episode(player, workspace: dict):
    """完整流程：填表 → 上传 → 合并 → 入库 → 自动切到新集（验收 §6 §7）。"""
    set_view(player, "lib")
    fill_import_form(player, workspace, IMPORT_TITLE, IMPORT_SEASON_EP)
    player.click("#impBtn")

    # 三阶段各自走到 done（用 attached：跑完会自动切到播放界面，节点就不可见了）
    player.wait_for_selector("#stUpload[data-state='done']", state="attached", timeout=60000)
    player.wait_for_selector("#stMerge[data-state='done']", state="attached", timeout=120000)
    player.wait_for_selector("#stIngest[data-state='done']", state="attached", timeout=60000)
    assert "导入完成" in player.text_content("#impMsg")

    # 自动切回播放界面，且选中的就是刚导入的那一集
    player.wait_for_selector("#view-play.on")
    assert player.evaluate("document.body.dataset.view") == "play"
    new_id = player.input_value("#ep")
    assert new_id != str(workspace["seeded_id"])
    assert IMPORT_SEASON_EP in player.locator("#ep option:checked").inner_text()

    # 合并出来的媒体真能播（vp8+vorbis 的 webm，chromium 解得了）
    player.wait_for_function("() => document.getElementById('video').readyState >= 1")
    assert player.evaluate("document.getElementById('video').duration") > 1

    # 字幕也进来了
    player.wait_for_function(
        "() => document.getElementById('status').textContent.indexOf('1 段') >= 0"
    )

    # 回内容库：新的一集在列表里，媒体状态是"有音轨"（合并成功）
    set_view(player, "lib")
    row = player.locator(f"#liblist .lrow[data-content-id='{new_id}']")
    assert row.count() == 1
    text = row.inner_text()
    assert IMPORT_TITLE in text and IMPORT_SEASON_EP in text
    assert "⚠" not in row.locator(".ms").inner_text()
    assert player.locator("#libcount").inner_text() == "2 集"

    # 服务端确实把媒体落在 data/library/<uuid>/ 里
    dirs = [p for p in workspace["library"].iterdir() if p.is_dir()]
    assert len(dirs) == 1
    assert any(p.name.startswith("merged") for p in dirs[0].iterdir())


def test_import_duplicate_is_refused_with_a_clear_message(player, workspace: dict):
    """重复导入（同标题+季集）当场拒绝，不动已有内容。"""
    set_view(player, "lib")
    before = player.locator("#liblist .lrow[data-content-id]").count()
    fill_import_form(player, workspace, IMPORT_TITLE, IMPORT_SEASON_EP)
    player.click("#impBtn")
    player.wait_for_selector("#impMsg.err", timeout=60000)
    msg = player.locator("#impMsg").inner_text()
    assert "已经导入过" in msg
    player.click("#librefresh")
    player.wait_for_timeout(200)
    assert player.locator("#liblist .lrow[data-content-id]").count() == before


def test_import_rejects_a_text_file_pretending_to_be_video(player, workspace: dict):
    fake = workspace["uploads"] / "fake.mp4"
    fake.write_text("这不是视频，只是个改了后缀的文本文件\n" * 40, encoding="utf-8")
    set_view(player, "lib")
    fill_import_form(player, workspace, "Bogus Show", "s09e09", audio=False, video=fake)
    player.click("#impBtn")
    player.wait_for_selector("#impMsg.err", timeout=60000)
    assert "不是可识别的媒体文件" in player.locator("#impMsg").inner_text()
    # 上传阶段是成功的，失败停在第二阶段
    assert player.get_attribute("#stUpload", "data-state") == "done"
    assert player.get_attribute("#stMerge", "data-state") == "err"
    player.click("#librefresh")
    player.wait_for_timeout(200)
    assert player.locator(
        "#liblist .lrow[data-content-id]"
    ).count() == 2  # 还是原来那两集


def test_import_silent_video_is_allowed_but_flagged(player, workspace: dict):
    """默片素材（无音轨）不拒绝，只在导入告警和内容库里标 ⚠ 无音轨。"""
    set_view(player, "lib")
    fill_import_form(player, workspace, "Silent Show", "s01e01", audio=False)
    player.click("#impBtn")
    player.wait_for_selector("#stIngest[data-state='done']", state="attached", timeout=90000)
    assert "无音轨" in player.text_content("#impWarn")

    player.wait_for_selector("#view-play.on")
    new_id = player.input_value("#ep")
    set_view(player, "lib")
    ms = player.locator(f"#liblist .lrow[data-content-id='{new_id}'] .ms")
    assert "⚠ 无音轨" in ms.inner_text()
    assert "warn" in (ms.get_attribute("class") or "")
