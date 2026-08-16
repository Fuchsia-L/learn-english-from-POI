"""player.html 端到端（DESIGN §4；M0 验收覆盖档 2）。

跑法:
    pytest tests/test_player_e2e.py          # 需要 ffmpeg + playwright chromium
    pytest -m "not slow"                     # 跳过本文件

版权红线（DESIGN §0 §6）：字幕、词框、词典、媒体全部自造——
srt 是自编句子，词框坐标是手编的假框，视频是 ffmpeg lavfi 生成的黑场，
词典用 build_ecdict 的 mini 夹具（100 词自造条目）。不含任何真实剧集素材。

夹具链路：自造 srt → app.ingest（含 --boxes-json 回填）→ uvicorn 起真服务
→ playwright chromium 打开 /static/player.html。
第 3 段故意不给词框，用来验证 DESIGN §4 的「对齐退路」真的实现了。
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app.ingest import ingest_srt, tokenize  # noqa: E402
from app.server import create_app  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg 造夹具视频"),
]

# --- 自造素材 --------------------------------------------------------------

SRT = """1
00:00:00,500 --> 00:00:03,000
The quiet cop began a stakeout near the door.

2
00:00:03,500 --> 00:00:06,000
My cousin wants to hire a new driver.

3
00:00:06,500 --> 00:00:09,000
We should check the water before we leave.
"""

SEG_TEXT = {
    1: "The quiet cop began a stakeout near the door.",
    2: "My cousin wants to hire a new driver.",
    3: "We should check the water before we leave.",
}
# 第 3 段不给词框 → 前端必须走自渲染退路
BOXED_IDX = (1, 2)

# 1080p 参考帧的字幕带（与 player.html 的 CONF 常量一致）
REF_W, REF_H = 1920, 1080
CN_BAND = (958, 1026)
EN_BAND = (1026, 1080)

# 词典释义里的分隔符是字面两个字符 "\n"（见 build_ecdict._clean_text），
# 前端必须 split 后逐行显示 —— 给 stakeout 塞一条两行释义来验。
STAKEOUT_GLOSS = "n. 盯梢；监视\\nv. 蹲守盯住"


def make_boxes(text: str, idx: int) -> dict:
    """按 token 顺序在英文行里排开的假词框（形状同 extract_hardsub --boxes-json）。"""
    words, x = [], 120
    for t in tokenize(text):
        raw = text[t.char_start : t.char_end]
        w = 22 * len(raw)
        words.append({"w": raw, "x": x, "y": EN_BAND[0] + 6, "width": w, "height": 40})
        x += w + 14
    return {"idx": idx, "start": 0.0, "end": 1.0, "text": text, "words": words}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("player_e2e")

    srt = d / "ep.srt"
    srt.write_text(SRT, encoding="utf-8")

    ecdict = d / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)
    c = sqlite3.connect(str(ecdict))
    c.execute(
        "UPDATE ecdict SET translation = ? WHERE word_lower = 'stakeout'",
        (STAKEOUT_GLOSS,),
    )
    c.commit()
    c.close()

    video = d / "black.webm"  # VP8：开源编码，headless chromium 一定能解
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=640x360:r=10:d=10",
         "-c:v", "libvpx", "-b:v", "80k", "-an", str(video)],
        check=True,
    )
    assert video.stat().st_size > 0

    boxes = d / "ep.boxes.json"
    boxes.write_text(
        json.dumps([make_boxes(SEG_TEXT[i], i) for i in BOXED_IDX], ensure_ascii=False),
        encoding="utf-8",
    )

    db = d / "poi.db"
    stats = ingest_srt(
        db_path=db, srt_path=srt, title="Fixture Show", season_ep="s01e01",
        video_path=str(video), boxes_path=boxes,
    )
    assert stats["segments"] == 3 and stats["boxes_applied"] == 2
    assert stats["segments_without_boxes"] == [3]
    return {"dir": d, "db": db, "ecdict": ecdict, "video": video}


@pytest.fixture(scope="module")
def server_url(workspace: dict):
    import uvicorn

    port = free_port()
    app = create_app(db_path=workspace["db"], ecdict_path=workspace["ecdict"])
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
    """打开播放器并等首帧数据就绪；顺带盯着 JS 报错（收场时断言为空）。"""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.on(
        "console",
        lambda m: errors.append("console: " + m.text) if m.type == "error" else None,
    )
    page.goto(server_url + "/static/player.html")
    page.wait_for_selector("#ep option[value]", state="attached")
    page.wait_for_function("() => document.getElementById('video').readyState >= 1")
    yield page
    assert errors == [], f"页面有 JS 报错: {errors}"


# --- 辅助 ------------------------------------------------------------------


def goto_segment(page, idx: int) -> None:
    """跳到第 idx 段中部并等前端把当前段切过去。"""
    t = {1: 1.5, 2: 4.5, 3: 7.5}[idx]
    page.evaluate(
        "t => { const v = document.getElementById('video'); v.pause(); v.currentTime = t; }", t
    )
    page.wait_for_function(
        "txt => document.getElementById('segtext').textContent === txt", arg=SEG_TEXT[idx]
    )


def picture_box(page) -> dict:
    """<video> 里画面的实际显示区域（object-fit:contain，自己按 16:9 算一遍）。"""
    b = page.locator("#video").bounding_box()
    w = min(b["width"], b["height"] * 16 / 9)
    h = min(b["height"], b["width"] * 9 / 16)
    return {"x": b["x"] + (b["width"] - w) / 2, "y": b["y"] + (b["height"] - h) / 2,
            "w": w, "h": h}


def approx(a: float, b: float, tol: float = 2.0) -> bool:
    return abs(a - b) <= tol


# --- 用例 ------------------------------------------------------------------


def test_page_lists_episodes_and_loads_segments(player):
    options = player.eval_on_selector_all("#ep option", "os => os.map(o => o.textContent)")
    assert len(options) == 1
    assert "s01e01" in options[0] and "Fixture Show" in options[0]
    assert "3 段" in player.inner_text("#status")
    assert player.evaluate("document.getElementById('video').duration") > 5
    # <video> 走的是 /media/{content_id}（Range 接口），不是 file://
    assert "/media/" in player.evaluate("document.getElementById('video').currentSrc")


def test_mode_two_is_default_and_masks_only_chinese_band(player):
    goto_segment(player, 1)
    assert player.inner_text("#modes .on").startswith("2")

    pic = picture_box(player)
    mask = player.locator("#mask").bounding_box()
    assert player.locator("#mask").is_visible()
    # 遮罩条只盖中文行 y∈[958,1026]
    assert approx(mask["x"], pic["x"]) and approx(mask["width"], pic["w"])
    assert approx(mask["y"], pic["y"] + CN_BAND[0] / REF_H * pic["h"])
    assert approx(mask["height"], (CN_BAND[1] - CN_BAND[0]) / REF_H * pic["h"])
    # 英文行没被遮，且原位可点
    assert player.locator("#hots .hot").count() > 0


def test_mode_one_shows_no_mask_and_mode_three_masks_both_rows(player):
    goto_segment(player, 1)
    n_hot = player.locator("#hots .hot").count()

    player.keyboard.press("1")                       # 档 1 双语：不遮挡，热区仍在
    player.wait_for_selector("#mask", state="hidden")
    assert player.locator("#hots .hot").count() == n_hot

    player.keyboard.press("3")                       # 档 3 裸听：中英全遮，无热区
    player.wait_for_selector("#mask", state="visible")
    pic = picture_box(player)
    mask = player.locator("#mask").bounding_box()
    assert approx(mask["y"], pic["y"] + CN_BAND[0] / REF_H * pic["h"])
    assert approx(mask["height"], (EN_BAND[1] - CN_BAND[0]) / REF_H * pic["h"])
    assert player.locator("#hots .hot").count() == 0
    assert not player.locator("#fbline").is_visible()
    # 裸听：文字面板也不许漏字幕
    assert SEG_TEXT[1] not in player.inner_text("#segline")

    player.keyboard.press("2")
    player.wait_for_selector("#mask", state="visible")
    assert player.locator("#hots .hot").count() == n_hot
    assert SEG_TEXT[1] in player.inner_text("#segline")


def test_hotspot_count_matches_current_segment_words(player):
    for idx in (1, 2):
        goto_segment(player, idx)
        words = [t.surface for t in tokenize(SEG_TEXT[idx])]
        hots = player.eval_on_selector_all(
            "#hots .hot", "ns => ns.map(n => n.dataset.surface.toLowerCase())"
        )
        assert hots == words, f"第 {idx} 段热区与词数/词序对不上"
        # 热区落在英文行上，且不越出画面
        pic = picture_box(player)
        for b in player.eval_on_selector_all(
            "#hots .hot", "ns => ns.map(n => n.getBoundingClientRect().toJSON())"
        ):
            assert b["y"] >= pic["y"] + (EN_BAND[0] - 20) / REF_H * pic["h"]
            assert b["x"] >= pic["x"] - 1
            assert b["x"] + b["width"] <= pic["x"] + pic["w"] + 1


def test_click_hotspot_opens_lookup_card(player):
    goto_segment(player, 1)
    player.click("#hots .hot[data-surface='stakeout']")
    player.wait_for_selector("#card.on")

    body = player.inner_text("#cardBody")
    assert "stakeout" in body                    # 当前形式 + 词元
    assert "ˈsteɪkaʊt" in body                   # 音标
    # 释义里的字面 "\n" 必须 split 后逐行显示
    lines = player.eval_on_selector_all("#cardBody .gloss div", "ns => ns.map(n => n.textContent)")
    assert lines == ["n. 盯梢；监视", "v. 蹲守盯住"]
    # 原句 + 点中的词高亮
    assert SEG_TEXT[1] in body.replace("\n", " ")
    assert player.inner_text("#cardBody .sent .tk.hl") == "stakeout"

    player.keyboard.press("Escape")
    player.wait_for_selector("#card.on", state="detached")


def test_collect_then_word_shows_up_in_vocab_sidebar(player, server_url):
    goto_segment(player, 1)
    player.click("#hots .hot[data-surface='cop']")
    player.wait_for_selector("#card.on")
    assert player.inner_text("#collectBtn") == "加入生词本"

    player.click("#collectBtn")
    player.wait_for_function(
        "() => document.getElementById('collectBtn').textContent === '已在生词本'"
    )
    assert player.locator("#collectBtn").is_disabled()

    player.keyboard.press("v")                    # 快捷键开生词本
    player.wait_for_selector("#vocab.on")
    player.wait_for_selector("#vlist .vcard")
    sidebar = player.inner_text("#vlist")
    assert "cop" in sidebar
    assert "警察" in sidebar                        # 词典释义
    assert SEG_TEXT[1] in sidebar.replace("\n", " ")  # encounter 原句
    assert "s01e01" in sidebar

    # 后端确实落库了（不只是前端画上去）
    with urllib.request.urlopen(server_url + "/vocab", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    lemmas = [v["lemma"] for v in data["vocab"]]
    assert "cop" in lemmas
    entry = next(v for v in data["vocab"] if v["lemma"] == "cop")
    assert entry["encounters"][0]["sentence"] == SEG_TEXT[1]

    player.keyboard.press("v")
    player.wait_for_selector("#vocab.on", state="detached")


def test_segment_without_boxes_falls_back_to_self_rendered_line(player):
    goto_segment(player, 3)
    assert player.locator("#hots .hot").count() == 0
    assert player.locator("#fbline").is_visible()

    words = [t.surface for t in tokenize(SEG_TEXT[3])]
    spans = player.eval_on_selector_all(
        "#fbline .tk", "ns => ns.map(n => n.dataset.surface)"
    )
    assert spans == words
    assert player.inner_text("#fbline").strip() == SEG_TEXT[3]

    # 退路下档 2 中英全遮（DESIGN §4：中英都遮 + 自渲染可点击英文字幕）
    pic = picture_box(player)
    mask = player.locator("#mask").bounding_box()
    assert approx(mask["height"], (EN_BAND[1] - CN_BAND[0]) / REF_H * pic["h"])
    # 自渲染的行在遮罩下沿
    fb = player.locator("#fbline").bounding_box()
    assert approx(fb["y"], pic["y"] + EN_BAND[0] / REF_H * pic["h"])

    player.click("#fbline .tk[data-surface='check']")
    player.wait_for_selector("#card.on")
    body = player.inner_text("#cardBody")
    assert "check" in body and "检查" in body
    assert player.inner_text("#cardBody .sent .tk.hl") == "check"


def test_click_pauses_and_escape_resumes_playback(player):
    goto_segment(player, 1)
    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")

    player.click("#hots .hot[data-surface='door']")
    player.wait_for_selector("#card.on")
    assert player.evaluate("document.getElementById('video').paused") is True

    player.keyboard.press("Escape")
    player.wait_for_selector("#card.on", state="detached")
    player.wait_for_function("() => !document.getElementById('video').paused")

    # 关掉「点词暂停」后不再打断播放
    player.click("#pauseChk")
    goto_segment(player, 1)
    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")
    player.click("#hots .hot[data-surface='quiet']")
    player.wait_for_selector("#card.on")
    assert player.evaluate("document.getElementById('video').paused") is False


def test_click_outside_card_closes_it(player):
    goto_segment(player, 2)
    player.click("#hots .hot[data-surface='cousin']")
    player.wait_for_selector("#card.on")
    player.click("#segline")
    player.wait_for_selector("#card.on", state="detached")


def test_no_browser_storage_api_is_used(player):
    """DESIGN §0 之外的硬约束：播放器不碰任何浏览器存储。"""
    src = Path(__file__).resolve().parents[1] / "app" / "static" / "player.html"
    text = src.read_text(encoding="utf-8")
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert banned not in text
    assert player.evaluate("window.localStorage.length") == 0
    assert player.evaluate("window.sessionStorage.length") == 0
