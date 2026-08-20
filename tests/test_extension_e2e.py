"""extension/ 划词插件端到端（工单 11）。

跑法:
    pytest tests/test_extension_e2e.py       # 需要 playwright chromium（新版无头）
    pytest -m "not slow"                     # 跳过本文件

链路：真 chromium（persistent context + --load-extension 加载 extension/ 解压目录）
→ 打开本地假页面 → **真实选区**（双击选词 / Range 跨行内标签）→ ⌖ 浮标 → 查询卡
→ 收藏 → 断言 SQLite 真落了库、卡片真变成 ✓。

版权红线（DESIGN §0 §6）：假页面上的英文全是自造句子，不含任何真实剧集台词。

**skip 纪律（工单 17-6）**：本文件**只有一个**允许 skip 的理由 —— 机器上压根没装
Playwright 的 chromium（executable 文件不存在）。除此之外一律 fail：浏览器在却起不来、
manifest 不合法、内容脚本没注入，全是产品自己的问题，不许伪装成"环境问题"跳过去
（跳过去的结果是 7 个用例既没跑也没红，还被当成"通过"写进文档）。

句子扩取的纯函数层另有 tests/test_extension_sentence.py 用 node 单测覆盖，
manifest 的形状在 tests/test_extension_manifest.py（那个连 node 都不需要）。
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402

from app.server import create_app  # noqa: E402

pytestmark = pytest.mark.slow

EXT_SRC = Path(__file__).resolve().parents[1] / "extension"

# --- 自造假页面（全部自编英文，注意 p2 故意把词拆在行内标签两边） -----------
PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture notes // POI</title></head>
<body>
<h1>Field notes</h1>
<p id="p1">The tired gardener began a <span id="w1">stakeout</span> near the
   <em>greenhouse</em> door. Nobody noticed him for hours.</p>
<p id="p2">A second <b>stake</b>out happened at dawn.</p>
<p id="p3">The quiet neighbour <span id="w3">Bramwell</span> never answered his phone.</p>
<p id="p4"><span id="w4">选中汉字不该弹浮标</span></p>
<p id="p5">My <span id="w5">cousins</span> bought two cameras.</p>
<p id="p6">The night <span id="w6">driver</span> waited by the quiet river.</p>
</body></html>
"""

P1_SENTENCE = "The tired gardener began a stakeout near the greenhouse door."
P2_SENTENCE = "A second stakeout happened at dawn."
P3_SENTENCE = "The quiet neighbour Bramwell never answered his phone."
P5_SENTENCE = "My cousins bought two cameras."

# p2 里 "stakeout" 被 <b> 劈成两半：跨行内标签拼句子的那条验收就靠它。
SELECT_SPLIT_WORD_JS = """() => {
  const b = document.querySelector('#p2 b');
  const after = b.nextSibling;                 // 文本节点 "out happened at dawn."
  const r = document.createRange();
  r.setStart(b.firstChild, 0);
  r.setEnd(after, 3);
  const s = getSelection();
  s.removeAllRanges();
  s.addRange(r);
  document.getElementById('p2').dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
}"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def make_ext(src: Path, dst: Path, api_base: str) -> Path:
    """把 extension/ 复制一份，只改 bg.js 里的服务地址常量（端口是随机的）。"""
    shutil.copytree(src, dst)
    bg = dst / "bg.js"
    text = bg.read_text(encoding="utf-8")
    patched, n = re.subn(
        r'const API_BASE = "[^"]*";', f'const API_BASE = "{api_base}";', text, count=1
    )
    # 常量改了名/换了写法就必须让测试炸，而不是默默测一个连不上的插件
    assert n == 1, "bg.js 里没找到 API_BASE 常量（改名了？）"
    bg.write_text(patched, encoding="utf-8")
    return dst


# --- 夹具 ------------------------------------------------------------------


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("ext_e2e")
    ecdict = d / "ecdict_mini.db"
    build_ecdict.build_mini(ecdict)
    return {"dir": d, "db": d / "poi.db", "ecdict": ecdict}


@pytest.fixture(scope="module")
def api_url(workspace: dict):
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


@pytest.fixture(scope="module")
def page_url(workspace: dict):
    """假网页用一个独立的静态服务托管：内容脚本只在 http(s) 页面上跑。"""
    root = workspace["dir"] / "site"
    root.mkdir(exist_ok=True)
    (root / "notes.html").write_text(PAGE_HTML, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None  # type: ignore[assignment]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/notes.html"
    httpd.shutdown()


def launch_with_extension(pw, user_data_dir: Path, ext_dir: Path):
    return pw.chromium.launch_persistent_context(
        str(user_data_dir),
        channel="chromium",  # 扩展要新版无头（老 headless shell 不加载扩展）
        headless=True,
        args=[
            f"--disable-extensions-except={ext_dir}",
            f"--load-extension={ext_dir}",
        ],
    )


def chromium_executable(pw) -> str:
    """Playwright 认的 chromium 可执行文件路径（拿不到就返回空串）。"""
    try:
        return str(pw.chromium.executable_path or "")
    except Exception:  # pragma: no cover - playwright 内部异常，当作"路径未知"
        return ""


INSTALL_HINT = (
    "PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers playwright install chromium"
)


@pytest.fixture(scope="module")
def pw():
    """整个模块共用一个 playwright 实例：sync API 不允许在自己的事件循环里再起一个。"""
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    yield p
    p.stop()


@pytest.fixture(scope="module")
def chromium_installed(pw) -> str:
    """**本文件唯一允许 skip 的地方**：浏览器二进制压根不在（工单 17-6）。

    只认"文件不存在"这一件事。存在却起不来 = 真问题，交给 ctx 去炸。
    """
    exe = chromium_executable(pw)
    if not exe or not Path(exe).exists():
        pytest.skip(
            f"没装 Playwright chromium：{exe or '路径未知'} 不存在；"
            f"跑 `{INSTALL_HINT}` 之后再来。"
            "（这是本文件唯一允许的 skip 理由：浏览器在却起不来 / manifest 不合法 / "
            "内容脚本没注入，一律算失败。）"
        )
    return exe


@pytest.fixture(scope="module")
def ctx(pw, chromium_installed: str, workspace: dict, api_url: str, page_url: str):
    """加载了插件的浏览器上下文。到这一步就不许再 skip 了 —— 只许成功或失败。"""
    ext = make_ext(EXT_SRC, workspace["dir"] / "ext_live", api_url)
    # manifest 不合法是产品问题：先自己看一眼，炸得比浏览器的沉默更有信息量
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("manifest_version") == 3, f"manifest 不是 MV3: {manifest}"
    bg = manifest.get("background") or {}
    assert bg.get("service_worker") and bg.get("scripts"), (
        f"background 少了 Chrome(service_worker) / Firefox(scripts) 中的一个键：{bg}"
    )

    # 浏览器**在**却起不来 = 失败，不是 skip（工单 17-6）
    context = launch_with_extension(pw, workspace["dir"] / "profile", ext)

    probe = context.new_page()
    probe.goto(page_url)
    probe.dblclick("#w6")
    try:
        probe.wait_for_selector(".poi-flag", timeout=8000)
    except Exception as exc:
        context.close()
        raise AssertionError(
            f"内容脚本没注入（{type(exc).__name__}）：选中 #w6 之后没等到 ⌖ 浮标。"
            f"chromium 在 {chromium_installed}，扩展已随 --load-extension 加载 —— "
            "这是扩展/注入本身的问题，不是环境问题，所以判失败而不是 skip（工单 17-6）。"
        ) from exc
    probe.close()

    yield context
    context.close()


@pytest.fixture()
def page(ctx, page_url: str):
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    p.goto(page_url)
    yield p
    assert errors == [], f"页面有 JS 报错: {errors}"
    p.close()


# --- 辅助 ------------------------------------------------------------------


def select_word(page, selector: str):
    """双击选词（真实鼠标事件）→ 等 ⌖ 浮标浮出来。"""
    page.dblclick(selector)
    page.wait_for_selector(".poi-flag", state="visible", timeout=6000)


def open_card(page):
    page.click(".poi-flag")
    page.wait_for_selector(".poi-card[data-state='done']", timeout=8000)


def card_text(page, selector: str) -> str:
    return (page.locator(selector).first.text_content() or "").strip()


def encounters(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT E.*, L.lemma FROM Encounter E "
        "JOIN VocabEntry V ON V.id = E.vocab_entry_id "
        "JOIN Lexeme L ON L.id = V.lexeme_id ORDER BY E.id"
    ).fetchall()
    conn.close()
    return rows


def rows_for(db: Path, lemma: str) -> list[sqlite3.Row]:
    return [r for r in encounters(db) if r["lemma"] == lemma]


# --- 用例 ------------------------------------------------------------------


def test_select_shows_flag_then_card_and_collects(page, workspace: dict, page_url: str):
    select_word(page, "#w5")
    assert page.locator(".poi-card").count() == 0  # 永不自动弹卡

    open_card(page)
    assert card_text(page, ".poi-surface") == "cousins"
    assert card_text(page, ".poi-lemma").startswith("cousin")
    assert card_text(page, ".poi-ipa") == "/ˈkʌzn/"
    assert "堂表兄弟姐妹" in card_text(page, ".poi-gloss")
    assert card_text(page, ".poi-sent") == P5_SENTENCE
    assert page.locator(".poi-note").count() == 0  # 词典收录了，没有告警行

    page.click(".poi-btn")
    page.wait_for_selector(".poi-btn.done", timeout=8000)
    assert "✓" in card_text(page, ".poi-btn")
    assert card_text(page, ".poi-msg") == "✓ 已收 · 1 次相遇"

    rows = rows_for(workspace["db"], "cousin")
    assert len(rows) == 1
    r = rows[0]
    assert r["surface"] == "cousins"
    assert r["source_kind"] == "web" and r["segment_id"] is None
    ctx_json = json.loads(r["context_json"])
    assert ctx_json["sentence"] == P5_SENTENCE
    assert ctx_json["url"] == page_url
    assert ctx_json["title"] == "Fixture notes // POI"


def test_reopening_a_collected_word_shows_check_and_count(page, workspace: dict):
    """第二次收同一个词只加相遇；重开卡直接是 ✓ 已收 · N 次相遇。"""
    select_word(page, "#w5")
    open_card(page)
    # 开卡就已收：状态写在这儿，但按钮不锁 —— 换个页面遇到同一个词是新的相遇
    assert card_text(page, ".poi-card[data-collected='1'] .poi-msg") == "✓ 已收 · 1 次相遇"
    assert not page.locator(".poi-btn").is_disabled()
    assert card_text(page, ".poi-btn") == "再记一次相遇"

    page.keyboard.press("Escape")
    page.wait_for_selector(".poi-card", state="detached")

    # 另一个词，收两次（两个不同的句子）：VocabEntry 只有一条，Encounter 两条
    page.evaluate(SELECT_SPLIT_WORD_JS)
    page.wait_for_selector(".poi-flag")
    open_card(page)
    assert card_text(page, ".poi-surface") == "stakeout"
    page.click(".poi-btn")
    page.wait_for_selector(".poi-btn.done")

    select_word(page, "#w1")
    open_card(page)
    page.click(".poi-btn")
    page.wait_for_selector(".poi-btn.done")
    assert card_text(page, ".poi-msg") == "✓ 已收 · 2 次相遇"

    rows = rows_for(workspace["db"], "stakeout")
    assert len(rows) == 2
    conn = sqlite3.connect(str(workspace["db"]))
    assert conn.execute(
        "SELECT COUNT(*) FROM VocabEntry V JOIN Lexeme L ON L.id = V.lexeme_id "
        "WHERE L.lemma='stakeout'"
    ).fetchone()[0] == 1
    conn.close()


def test_sentence_is_stitched_across_inline_tags(page, workspace: dict):
    """句子扩取在 DOM 层：<b>stake</b>out 要拼成一个词，句子扩到句号为止。"""
    page.evaluate(SELECT_SPLIT_WORD_JS)
    page.wait_for_selector(".poi-flag")
    open_card(page)
    assert card_text(page, ".poi-surface") == "stakeout"
    assert card_text(page, ".poi-sent") == P2_SENTENCE

    page.keyboard.press("Escape")
    select_word(page, "#w1")
    open_card(page)
    # 跨 <em>，且在句号处停住（不吞下一句）
    assert card_text(page, ".poi-sent") == P1_SENTENCE


def test_word_not_in_dictionary_is_flagged_but_collectable(page, workspace: dict):
    select_word(page, "#w3")
    open_card(page)
    assert card_text(page, ".poi-surface") == "bramwell"
    assert "词典未收录" in card_text(page, ".poi-note")
    assert card_text(page, ".poi-sent") == P3_SENTENCE
    assert not page.locator(".poi-btn").is_disabled()

    page.click(".poi-btn")
    page.wait_for_selector(".poi-btn.done")
    rows = rows_for(workspace["db"], "bramwell")
    assert len(rows) == 1 and rows[0]["source_kind"] == "web"


def test_flag_only_for_english_and_vanishes_after_timeout(page):
    page.dblclick("#w4")  # 汉字选区
    page.wait_for_timeout(600)
    assert page.locator(".poi-flag").count() == 0

    select_word(page, "#w6")
    page.wait_for_selector(".poi-flag", state="detached", timeout=6000)  # 3 秒自动消失
    assert page.locator(".poi-card").count() == 0


def test_escape_and_outside_click_close_the_card(page):
    select_word(page, "#w6")
    open_card(page)
    page.keyboard.press("Escape")
    page.wait_for_selector(".poi-card", state="detached")

    select_word(page, "#w6")
    open_card(page)
    page.mouse.click(5, 5)  # 点卡外
    page.wait_for_selector(".poi-card", state="detached")


def test_offline_shows_one_line_hint(pw, ctx, workspace: dict, page_url: str):
    """本地服务没起：卡里一行小字，不报错刷屏。

    ctx 夹具只为"扩展加载得起来"这个前提而依赖（它跳，这条也跳）；
    这里另起一个上下文，装的是把 API_BASE 指到死端口的那份插件副本。
    """
    dead = f"http://127.0.0.1:{free_port()}"  # 没人监听的端口
    ext = make_ext(EXT_SRC, workspace["dir"] / "ext_offline", dead)
    context = launch_with_extension(pw, workspace["dir"] / "profile_offline", ext)
    try:
        p = context.new_page()
        errors: list[str] = []
        p.on("pageerror", lambda e: errors.append(str(e)))
        p.goto(page_url)
        p.dblclick("#w6")
        p.wait_for_selector(".poi-flag", timeout=8000)
        p.click(".poi-flag")
        p.wait_for_selector(".poi-card[data-state='offline']", timeout=10000)
        note = (p.locator(".poi-note").first.text_content() or "").strip()
        assert "本地服务未启动" in note
        assert "uvicorn app.server:app" in note
        assert errors == []
    finally:
        context.close()
