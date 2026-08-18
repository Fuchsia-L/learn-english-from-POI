"""player.html 端到端（DESIGN §4；M0 验收覆盖档 2）。

跑法:
    pytest tests/test_player_e2e.py          # 需要 ffmpeg + playwright chromium
    pytest -m "not slow"                     # 跳过本文件

版权红线（DESIGN §0 §6）：字幕、词框、词典、媒体全部自造——
srt 是自编句子，词框坐标是手编的假框，视频是 ffmpeg lavfi 合成的彩色测试图
（testsrc2：遮罩要验"不是黑条"，纯黑视频验不出来），
词典用 build_ecdict 的 mini 夹具（100 词自造条目）。不含任何真实剧集素材。

夹具链路：自造 srt → app.ingest（含 --boxes-json 回填）→ uvicorn 起真服务
→ playwright chromium 打开 /static/player.html。
第 3 段故意不给词框，用来验证 DESIGN §4 的「对齐退路」真的实现了。
助记用例跑 fake provider 的 worker（离线、0 元、确定性输出）。

**反 flake 约定**：凡是断言查询卡内容的用例，必须先等 `#card.on[data-state=done]`
（data-state 是卡片的终态标志），不能只等 `#card.on` —— 只等 on 会读到"查询中…"。
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

from app.annotate import AnnotateWorker  # noqa: E402
from app.ingest import ingest_srt, tokenize  # noqa: E402
from app.providers.fake import GLOSS_PREFIX, FakeProvider  # noqa: E402
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
CN_BAND = (958, 1030)
EN_BAND = (1026, 1080)

# 词典释义里的分隔符是字面两个字符 "\n"（见 build_ecdict._clean_text），
# 前端必须 split 后逐行显示 —— 给 stakeout 塞一条两行释义来验。
# 这条释义还会被 fake provider 抄进 context_gloss，顺带验助记正文也按字面 \n 拆行。
STAKEOUT_GLOSS = "n. 盯梢；监视\\nv. 蹲守盯住"

# --- 暗场景夹具（真实片段回归：暗背景 + 白字最容易把遮罩糊成亮条） ----------
# 第二集：0~3s 是亮灰场（seek 前的"旧帧"），3s 后是近黑场 + 烧录白字幕。
# 中文行用拼音占位（容器里未必有中日韩字体，字形不重要，白色亮块才是被测对象）。
DARK_SRT = """1
00:00:04,000 --> 00:00:07,500
The night driver waited by the quiet river.
"""
DARK_TEXT = "The night driver waited by the quiet river."
DARK_CN_LINE = "ZHONGWEN ZIMU ZAI ZHE YI HANG"
DARK_W, DARK_H = 960, 540          # 16:9，与 1080p 参考帧同比
DARK_BRIGHT_T = 1.0                # 亮场取样点
DARK_DARK_T = 5.5                  # 暗场取样点（字幕在画面上）


def make_boxes(text: str, idx: int) -> dict:
    """按 token 顺序在英文行里排开的假词框（形状同 extract_hardsub --boxes-json）。"""
    words, x = [], 120
    for t in tokenize(text):
        raw = text[t.char_start : t.char_end]
        w = 22 * len(raw)
        words.append({"w": raw, "x": x, "y": EN_BAND[0] + 6, "width": w, "height": 40})
        x += w + 14
    return {"idx": idx, "start": 0.0, "end": 1.0, "text": text, "words": words}


def make_dark_video(path: Path) -> Path:
    """0~3s 亮灰场 → 3~8s 近黑场 + 烧录白字幕（位置按 1080p 参考帧等比换算）。

    亮场那 3 秒是故意的：seek 到暗场时解码器还在吐亮帧，
    遮罩要是把那张"脏帧"缓存下来，暗夜戏里就会糊出一条亮白涂抹。
    """
    def y_of(ref_y: int) -> int:
        return round(ref_y / REF_H * DARK_H)

    cn_y, en_y = y_of(CN_BAND[0]) + 2, y_of(EN_BAND[0]) + 2
    draw = (
        f"drawtext=text='{DARK_CN_LINE}':fontcolor=white:fontsize=26:"
        f"x=(w-text_w)/2:y={cn_y}:borderw=2:bordercolor=black,"
        f"drawtext=text='{DARK_TEXT}':fontcolor=white:fontsize=22:"
        f"x=(w-text_w)/2:y={en_y}:borderw=2:bordercolor=black"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-t", "3", "-i", f"color=c=0xb4b4b4:s={DARK_W}x{DARK_H}:r=10",
         "-f", "lavfi", "-t", "5", "-i", f"color=c=0x0a0a0a:s={DARK_W}x{DARK_H}:r=10",
         "-filter_complex", f"[1:v]{draw}[d];[0:v][d]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-c:v", "libvpx", "-b:v", "900k", "-an", str(path)],
        check=True,
    )
    assert path.stat().st_size > 0
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

    # VP8：开源编码，headless chromium 一定能解。
    # 用 testsrc2（彩色合成图）而不是黑场：遮罩用例要读 canvas 像素断言
    # "不是黑条 + 有画面内容"，纯黑视频这条根本验不出来。
    video = d / "color.webm"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=s=640x360:r=10:d=10",
         "-c:v", "libvpx", "-b:v", "600k", "-an", str(video)],
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

    # 第二集：暗场景 + 烧录白字幕（遮罩回归用）
    dark_srt = d / "dark.srt"
    dark_srt.write_text(DARK_SRT, encoding="utf-8")
    dark_video = make_dark_video(d / "dark.webm")
    dark_boxes = d / "dark.boxes.json"
    dark_boxes.write_text(
        json.dumps([make_boxes(DARK_TEXT, 1)], ensure_ascii=False), encoding="utf-8"
    )
    # 同一部剧的第二集：/episodes 按 (title, season_ep) 排序，
    # 这样它稳定排在 s01e01 后面，播放器默认仍然落在第一集
    dstats = ingest_srt(
        db_path=db, srt_path=dark_srt, title="Fixture Show", season_ep="s01e02",
        video_path=str(dark_video), boxes_path=dark_boxes,
    )
    assert dstats["segments"] == 1 and dstats["boxes_applied"] == 1

    return {"dir": d, "db": db, "ecdict": ecdict, "video": video, "dark": dark_video}


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


def open_card(page, selector: str) -> None:
    """点开查询卡并等到查询真的完成（data-state=done），杜绝读到"查询中…"。"""
    page.click(selector)
    page.wait_for_selector("#card.on[data-state='done']")


def set_view(page, view: str) -> None:
    page.click(f"#tabs button[data-view='{view}']")
    page.wait_for_selector(f"#view-{view}.on")


# 遮罩画布的读数：带内平均亮度、最亮像素，以及"带外背景"（带上方 24 参考行）
MASK_PROBE_JS = """() => {
  const cv = document.getElementById('maskcv');
  const v = document.getElementById('video');
  const lum = (a, i) => 0.299*a[i] + 0.587*a[i+1] + 0.114*a[i+2];
  const pad = Number(cv.dataset.pad || 1);
  const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
  let sum = 0, n = 0, max = 0;
  for (let y = pad; y < cv.height; y++) {
    for (let x = 0; x < cv.width; x++) {
      const l = lum(d, (y*cv.width + x)*4);
      sum += l; n++; if (l > max) max = l;
    }
  }
  // 带外背景：遮罩带上方 [928,952) 参考行，直接从 <video> 采（那儿没有字）
  const t = document.createElement('canvas'); t.width = 60; t.height = 8;
  const tx = t.getContext('2d');
  tx.drawImage(v, 0, 928/1080*v.videoHeight, v.videoWidth, 24/1080*v.videoHeight,
               0, 0, 60, 8);
  const td = tx.getImageData(0, 0, 60, 8).data;
  let osum = 0;
  for (let i = 0; i < td.length; i += 4) osum += lum(td, i);
  return {band: sum/n, outside: osum/(td.length/4), max: max,
          pad: pad, h: cv.height, frames: Number(cv.dataset.frames || 0),
          stats: window.__maskStats};
}"""

# 画布带内平均亮度（wait_for_function 用的精简版）
MASK_BAND_MEAN_JS = """() => {
  const cv = document.getElementById('maskcv');
  if (cv.dataset.painted !== '1') return -1;
  const pad = Number(cv.dataset.pad || 1);
  const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
  let s = 0, n = 0;
  for (let y = pad; y < cv.height; y++) for (let x = 0; x < cv.width; x++) {
    const i = (y*cv.width + x)*4;
    s += 0.299*d[i] + 0.587*d[i+1] + 0.114*d[i+2]; n++;
  }
  return s/n;
}"""


def seek(page, t: float) -> None:
    page.evaluate(
        "t => { const v = document.getElementById('video'); v.pause(); v.currentTime = t; }", t
    )


def run_fake_worker(workspace: dict) -> dict:
    """离线跑一轮助记 worker（fake provider：0 元、0 网络、输出确定）。"""
    with AnnotateWorker(
        db_path=workspace["db"],
        provider=FakeProvider(),
        ecdict_path=workspace["ecdict"],
        log=lambda msg: None,
    ) as w:
        return w.run_once().as_dict()


# --- 用例 ------------------------------------------------------------------


def test_page_lists_episodes_and_loads_segments(player):
    options = player.eval_on_selector_all("#ep option", "os => os.map(o => o.textContent)")
    assert len(options) == 2                       # 正片夹具 + 暗场景夹具
    assert "s01e01" in options[0] and "Fixture Show" in options[0]
    assert "s01e02" in options[1]
    assert player.locator("#ep").input_value() == player.eval_on_selector_all(
        "#ep option", "os => os[0].value"
    )                                              # 默认落在第一集
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
    open_card(player, "#hots .hot[data-surface='stakeout']")

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


def test_collect_then_word_shows_up_in_vocab_view(player, server_url):
    goto_segment(player, 1)
    open_card(player, "#hots .hot[data-surface='cop']")
    assert player.inner_text("#collectBtn") == "加入生词本"

    player.click("#collectBtn")
    player.wait_for_function(
        "() => document.getElementById('collectBtn').textContent === '已在生词本'"
    )
    assert player.locator("#collectBtn").is_disabled()

    player.keyboard.press("v")                    # 快捷键切界面（不再是侧栏）
    player.wait_for_selector("#view-vocab.on")
    player.wait_for_selector("#vlist .vcard")
    grid = player.inner_text("#vlist")
    assert "cop" in grid
    assert "警察" in grid                            # 词典释义
    assert SEG_TEXT[1] in grid.replace("\n", " ")     # encounter 原句
    assert "s01e01" in grid

    # 后端确实落库了（不只是前端画上去）
    with urllib.request.urlopen(server_url + "/vocab", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    lemmas = [v["lemma"] for v in data["vocab"]]
    assert "cop" in lemmas
    entry = next(v for v in data["vocab"] if v["lemma"] == "cop")
    assert entry["encounters"][0]["sentence"] == SEG_TEXT[1]

    player.keyboard.press("v")
    player.wait_for_selector("#view-play.on")


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

    open_card(player, "#fbline .tk[data-surface='check']")
    body = player.inner_text("#cardBody")
    assert "check" in body and "检查" in body
    assert player.inner_text("#cardBody .sent .tk.hl") == "check"


def test_click_pauses_and_escape_resumes_playback(player):
    goto_segment(player, 1)
    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")

    open_card(player, "#hots .hot[data-surface='door']")
    assert player.evaluate("document.getElementById('video').paused") is True

    player.keyboard.press("Escape")
    player.wait_for_selector("#card.on", state="detached")
    player.wait_for_function("() => !document.getElementById('video').paused")

    # 关掉「点词暂停」后不再打断播放
    player.click("#pauseChk")
    goto_segment(player, 1)
    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")
    open_card(player, "#hots .hot[data-surface='quiet']")
    assert player.evaluate("document.getElementById('video').paused") is False


def test_space_toggles_play_after_seekbar_drag(player):
    """拖过进度条之后，空格还得是播放/暂停（焦点留在 <input type=range> 上的坑）。"""
    goto_segment(player, 1)
    box = player.locator("#seek").bounding_box()
    y = box["y"] + box["height"] / 2
    player.mouse.move(box["x"] + box["width"] * 0.08, y)
    player.mouse.down()                                  # 真实拖动：焦点会落到滑块上
    player.mouse.move(box["x"] + box["width"] * 0.20, y, steps=8)
    player.mouse.up()
    player.wait_for_function("() => document.getElementById('video').currentTime > 0.5")
    # 拖完焦点必须交还，别让滑块继续截快捷键
    player.wait_for_function("() => document.activeElement.id !== 'seek'")

    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")
    player.keyboard.press(" ")                           # 拖动之后：空格 → 暂停
    player.wait_for_function("() => document.getElementById('video').paused")
    player.keyboard.press(" ")                           # 再按 → 恢复播放
    player.wait_for_function("() => !document.getElementById('video').paused")

    # 焦点被强行留在滑块上（Tab 过去也是这样）时，空格同样要生效
    player.focus("#seek")
    assert player.evaluate("document.activeElement.id") == "seek"
    player.keyboard.press(" ")
    player.wait_for_function("() => document.getElementById('video').paused")
    player.keyboard.press(" ")
    player.wait_for_function("() => !document.getElementById('video').paused")
    player.evaluate("document.getElementById('video').pause()")


def test_click_outside_card_closes_it(player):
    goto_segment(player, 2)
    open_card(player, "#hots .hot[data-surface='cousin']")
    player.click("#segline")
    player.wait_for_selector("#card.on", state="detached")


def test_view_tabs_switch_and_video_state_is_restored(player):
    """播放是主界面，生词本是另一个界面；切走暂停、切回恢复原状态。"""
    tabs = player.eval_on_selector_all("#tabs button", "bs => bs.map(b => b.textContent)")
    assert tabs == ["[ 播放 ]", "[ 生词本 ]"]
    assert player.locator("#view-play").is_visible()
    assert player.locator("#view-vocab").is_hidden()

    goto_segment(player, 1)
    player.evaluate("document.getElementById('video').play()")
    player.wait_for_function("() => !document.getElementById('video').paused")

    set_view(player, "vocab")                       # 切走：自动暂停
    assert player.locator("#view-play").is_hidden()
    assert player.evaluate("document.getElementById('video').paused") is True
    # 生词本界面是全屏的，播放界面的顶栏控件跟着收起
    assert player.locator("#modes").is_hidden()
    assert player.locator("#ep").is_hidden()

    player.keyboard.press("v")                      # v 切回：恢复播放
    player.wait_for_selector("#view-play.on")
    player.wait_for_function("() => !document.getElementById('video').paused")

    player.evaluate("document.getElementById('video').pause()")
    player.keyboard.press("v")
    player.wait_for_selector("#view-vocab.on")
    player.keyboard.press("v")
    player.wait_for_selector("#view-play.on")
    # 切走前是暂停的，切回来就不许自己播起来
    assert player.evaluate("document.getElementById('video').paused") is True

    # 侧栏彻底删掉了（不是藏起来）
    assert player.locator("#vocab").count() == 0


def test_vocab_view_renders_full_cards(player):
    goto_segment(player, 2)
    open_card(player, "#hots .hot[data-surface='hire']")
    player.click("#collectBtn")
    player.wait_for_function(
        "() => document.getElementById('collectBtn').textContent === '已在生词本'"
    )

    set_view(player, "vocab")
    player.wait_for_selector("#vlist .vcard")
    card = player.locator("#vlist .vcard").filter(has_text="hire").first
    assert "ˈhaɪə" in card.inner_text()              # 音标
    assert "雇用" in card.inner_text()                # 词典释义
    # encounters 在宽屏里全展开：集数 + 时间 + 原形 + 原句
    assert card.locator(".enc").count() >= 1
    enc = card.locator(".enc").first.inner_text()
    assert "s01e01" in enc and SEG_TEXT[2] in enc.replace("\n", " ")
    # 栅格布局（不是侧栏那种单列窄条）
    assert player.evaluate(
        "getComputedStyle(document.getElementById('vlist')).display"
    ) == "grid"
    assert player.locator("#vcount").inner_text().endswith("次相遇")


def test_mnemonic_polls_and_renders_in_card_and_vocab(player, workspace):
    """DESIGN §5：助记异步生成，前端轮询 /mnemonic 展示 gloss + hooks。"""
    goto_segment(player, 1)
    # 用 stakeout：它的词典释义是两行（字面 "\n" 分隔），fake provider 会把这条
    # 释义抄进 context_gloss —— 正好验助记正文也按字面 \n 拆行，不许原样吐出来
    open_card(player, "#hots .hot[data-surface='stakeout']")
    player.click("#collectBtn")
    player.wait_for_function(
        "() => document.getElementById('collectBtn').textContent === '已在生词本'"
    )
    # 还没跑 worker：显示"助记生成中…"
    player.wait_for_selector("#cardMnemo[data-state='annotating']")
    assert "助记生成中" in player.inner_text("#cardMnemo")

    stats = run_fake_worker(workspace)              # 离线跑一轮假 worker
    assert stats["done"] >= 1 and stats["failed"] == 0

    # 卡片轮询（2s 一次）应该自己翻成 done，不用手动刷新
    player.wait_for_selector("#cardMnemo[data-state='done']", timeout=15000)
    body = player.inner_text("#cardMnemo")
    assert GLOSS_PREFIX in body                      # 语境释义（fake 的可辨识前缀）
    assert player.locator("#cardMnemo .hook").count() >= 1
    assert player.locator("#cardMnemo .hook .badge").first.inner_text() == "morph"
    assert "未经词源核验" in body                      # 免责标签必须在场（DESIGN §5）
    # 助记正文按字面 "\n" 拆行（provider 把两行词典释义抄进了 context_gloss）
    assert "\\n" not in body, "助记里的字面 \\n 没拆行"
    assert "盯梢；监视" in body and "蹲守盯住" in body
    gloss_text = player.inner_text("#cardMnemo .mgloss")
    assert gloss_text.count("\n") >= 1
    # 免责标签是小字弱化显示，不许跟正文一样重
    sizes = player.evaluate(
        "() => { const h = document.querySelector('#cardMnemo .hook');"
        " return [parseFloat(getComputedStyle(h.querySelector('.htext')).fontSize),"
        " parseFloat(getComputedStyle(h.querySelector('.lbl')).fontSize)]; }"
    )
    assert sizes[1] < sizes[0]

    # 生词本界面：打开时对可见卡片各拉一次（不轮询）
    set_view(player, "vocab")
    player.wait_for_selector("#vlist .vcard .mnemo[data-state='done']")
    vcard = player.locator("#vlist .vcard").filter(has_text="stakeout").first
    vmnemo = vcard.locator(".mnemo").inner_text()
    assert GLOSS_PREFIX in vmnemo
    assert vcard.locator(".mnemo .hook").count() >= 1
    assert "\\n" not in vmnemo, "生词本助记里的字面 \\n 没拆行"


def test_mask_is_natural_blur_not_a_black_bar(player):
    """遮罩不是黑条：canvas 里是画面降采样 + 高亮抑制的结果，亮度有方差。"""
    goto_segment(player, 1)
    player.wait_for_function(
        "() => document.getElementById('maskcv').dataset.painted === '1'"
    )
    m = player.evaluate(
        """() => {
          const cv = document.getElementById('maskcv');
          const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
          const n = d.length / 4;
          let sum = 0, sum2 = 0, mx = 0;
          for (let i = 0; i < d.length; i += 4) {
            const l = 0.299*d[i] + 0.587*d[i+1] + 0.114*d[i+2];
            sum += l; sum2 += l*l; if (l > mx) mx = l;
          }
          const mean = sum / n;
          const st = getComputedStyle(document.getElementById('mask'));
          return {mean, variance: sum2/n - mean*mean, max: mx, w: cv.width, h: cv.height,
                  bg: st.backgroundColor, filter: getComputedStyle(cv).filter,
                  stats: window.__maskStats};
        }"""
    )
    # 大幅降采样（1920/16 量级），不是原尺寸逐像素处理
    assert 8 <= m["w"] <= 320 and 3 <= m["h"] <= 64
    assert m["mean"] > 8, f"遮罩几乎全黑，又变回黑条了: {m}"
    assert m["variance"] > 4, f"遮罩没有画面内容（纯色块）: {m}"
    assert "blur" in m["filter"]                    # 叠了模糊平滑块感
    # 底色不许是黑条：alpha 上限 .35
    alpha = float(m["bg"].split(",")[-1].rstrip(")")) if m["bg"].startswith("rgba") else 1.0
    assert alpha <= 0.35, m["bg"]
    assert m["stats"]["mode"] == "canvas"

    # 暂停状态下 seek 也要刷新一帧（不是只在播放时更新）
    frames = player.evaluate("Number(document.getElementById('maskcv').dataset.frames)")
    goto_segment(player, 2)
    player.wait_for_function(
        "n => Number(document.getElementById('maskcv').dataset.frames) > n", arg=frames
    )
    # 性能红线：设计目标每帧 <3ms，这里给足 CI 余量、只挡住数量级跑偏
    # （首帧含 JIT/分配不进均值，所以这里读的是稳态）
    stats = player.evaluate("window.__maskStats")
    assert 0 < stats["avgMs"] < 15, stats
    assert stats["lastMs"] < 15, stats

    # 档 3 遮整条：canvas 跟着变高
    h2 = player.evaluate("document.getElementById('maskcv').height")
    player.keyboard.press("3")
    player.wait_for_function("h => document.getElementById('maskcv').height > h", arg=h2)
    player.keyboard.press("2")


def test_dark_scene_mask_stays_at_background_level(player):
    """暗背景 + 白字（真实片段回归）：遮罩带亮度必须贴着带外背景，不许糊出亮条。

    白字亮度混进背景估计、或 seek 后把上一帧（亮场）缓存下来，都会在这里现原形。
    """
    dark = [
        o for o in player.eval_on_selector_all(
            "#ep option", "os => os.map(o => ({v: o.value, t: o.textContent}))"
        ) if "s01e02" in o["t"]
    ]
    assert dark, "暗场景夹具那一集没进选集列表"
    player.select_option("#ep", value=dark[0]["v"])
    player.wait_for_function("() => document.getElementById('video').readyState >= 1")

    # 1) 亮场：遮罩跟着画面走，读数应该很亮
    seek(player, DARK_BRIGHT_T)
    player.wait_for_function(f"() => ({MASK_BAND_MEAN_JS})() > 100", timeout=15000)

    # 2) seek 到暗场：解码器还在吐亮帧时画的那张不算数，必须跟到新帧
    #    （回归点：旧实现按 currentTime 判重，会把亮帧永久缓存 → 一条亮白涂抹）
    seek(player, DARK_DARK_T)
    player.wait_for_function(
        f"() => {{ const m = ({MASK_BAND_MEAN_JS})(); return m >= 0 && m < 40; }}",
        timeout=15000,
    )

    m = player.evaluate(MASK_PROBE_JS)
    # 带内 ≈ 带外背景（近黑场：两边都该是 10 上下）
    assert abs(m["band"] - m["outside"]) < 10, m
    # 白字（255）绝不能留下亮块
    assert m["max"] < 70, m
    assert m["stats"]["mode"] == "canvas"


def test_no_browser_storage_api_is_used(player):
    """DESIGN §0 之外的硬约束：播放器不碰任何浏览器存储。"""
    src = Path(__file__).resolve().parents[1] / "app" / "static" / "player.html"
    text = src.read_text(encoding="utf-8")
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert banned not in text
    assert player.evaluate("window.localStorage.length") == 0
    assert player.evaluate("window.sessionStorage.length") == 0
