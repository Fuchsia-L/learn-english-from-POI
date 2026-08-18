"""extension/content.js 里句子扩取的纯函数层（工单 11）。

为什么是 node 而不是 pytest 直接测：这段逻辑必须和插件里跑的是**同一份代码**，
在 Python 里重写一遍等于养两套实现，迟早对不上。content.js 因此写成
"有 document 才装事件、有 module 才导出"，node 能直接 require 它拿到纯函数
（DOM 那层在 tests/test_extension_e2e.py 里用真浏览器验）。

夹具全是自造英文句子（DESIGN §0 §6），不含任何真实剧集台词。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONTENT_JS = Path(__file__).resolve().parents[1] / "extension" / "content.js"

pytestmark = pytest.mark.skipif(
    NODE is None, reason="需要 node 才能跑 extension/content.js 的纯函数"
)

DRIVER_JS = """
const fs = require('fs');
const mod = require(process.argv[2]);
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map((c) => {
  if (c.fn === 'slice') return mod.sliceSentence(c.text, c.start, c.end);
  if (c.fn === 'english') return mod.looksEnglish(c.text);
  if (c.fn === 'isEnd') return mod.isSentenceEnd(c.text, c.i);
  if (c.fn === 'max') return mod.MAX_SENTENCE_CHARS;
  throw new Error('unknown fn ' + c.fn);
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    d = tmp_path_factory.mktemp("ext_js")
    driver = d / "driver.js"
    driver.write_text(DRIVER_JS, encoding="utf-8")

    def run(cases: list[dict]):
        cases_file = d / "cases.json"
        cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(driver), str(CONTENT_JS), str(cases_file)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def sentence(js, text: str, word: str) -> str:
    """在 text 里选中 word（首次出现），返回扩取到的整句。"""
    start = text.index(word)
    return js([{"fn": "slice", "text": text, "start": start, "end": start + len(word)}])[0]


# --- 基本边界 --------------------------------------------------------------


def test_picks_the_sentence_the_selection_sits_in(js):
    text = (
        "The gardener went home early. The tall cat slept on the porch. "
        "Nobody moved for hours."
    )
    assert sentence(js, text, "cat") == "The tall cat slept on the porch."
    assert sentence(js, text, "gardener") == "The gardener went home early."
    assert sentence(js, text, "hours") == "Nobody moved for hours."


def test_question_and_exclamation_are_boundaries(js):
    text = "Who moved the camera? Nobody answered! The room stayed quiet."
    assert sentence(js, text, "camera") == "Who moved the camera?"
    assert sentence(js, text, "answered") == "Nobody answered!"
    assert sentence(js, text, "quiet") == "The room stayed quiet."


def test_newline_is_a_hard_boundary(js):
    """拍平 DOM 时块级标签之间插的是 "\\n"：段落绝不允许粘连。"""
    text = "The first paragraph ends without punctuation\nA second paragraph begins here"
    assert sentence(js, text, "second") == "A second paragraph begins here"
    assert sentence(js, text, "first") == "The first paragraph ends without punctuation"


def test_selection_spanning_two_sentences_keeps_both(js):
    text = "Alpha runs fast. Beta walks slow. Gamma sleeps."
    start = text.index("fast")
    end = text.index("Beta") + len("Beta")
    got = js([{"fn": "slice", "text": text, "start": start, "end": end}])[0]
    assert got == "Alpha runs fast. Beta walks slow."


def test_trailing_quote_stays_with_the_sentence(js):
    text = 'He said "Go home now." Then he left the building.'
    assert sentence(js, text, "home") == 'He said "Go home now."'
    assert sentence(js, text, "building") == "Then he left the building."


def test_whitespace_is_collapsed(js):
    text = "  The   quiet   cop  waited.   Nobody   came.  "
    assert sentence(js, text, "cop") == "The quiet cop waited."


# --- 点号不是句号的那些情况 ------------------------------------------------


def test_decimal_point_is_not_a_boundary(js):
    text = "The lens costs 3.5 dollars today. Nobody cares."
    assert sentence(js, text, "lens") == "The lens costs 3.5 dollars today."


def test_domain_name_is_not_a_boundary(js):
    text = "Visit example.com for the details. Then leave the page."
    assert sentence(js, text, "details") == "Visit example.com for the details."


def test_common_abbreviation_is_not_a_boundary(js):
    text = "Mr. Halloway waited by the gate. The rain finally stopped."
    assert sentence(js, text, "gate") == "Mr. Halloway waited by the gate."
    text2 = "The parcel weighed 2 kg, etc. Nobody signed for it."
    assert sentence(js, text2, "parcel").startswith("The parcel weighed")


def test_single_letter_initials_are_not_boundaries(js):
    text = "The letter from J. K. Marlow arrived late. It was empty."
    assert sentence(js, text, "arrived") == "The letter from J. K. Marlow arrived late."


def test_inner_dotted_abbreviation_is_conservative(js):
    """e.g. / i.e. 里的点一律不当句号 —— 代价是句末的 "p.m." 会把下一句带上。

    这是**有意选的方向**：宁可多给一句上下文，也不要把句子从中间劈开。
    """
    text = "Bring a torch, e.g. the small one. Nobody has spares."
    assert sentence(js, text, "torch").startswith("Bring a torch, e.g. the small one.")
    late = "She called at 4 p.m. Nobody picked up."
    assert sentence(js, late, "called") == "She called at 4 p.m. Nobody picked up."


def test_letter_glued_after_period_is_not_a_boundary(js):
    text = "The file name is note.txt and it is empty. Delete it later."
    assert sentence(js, text, "name") == "The file name is note.txt and it is empty."


# --- 兜底：没有边界 / 越界 / 空输入 ----------------------------------------


def test_long_text_without_boundaries_is_capped_at_word_edges(js):
    limit = js([{"fn": "max"}])[0]
    text = ("word " * 400).strip()
    start = len(text) // 2
    got = js([{"fn": "slice", "text": text, "start": start, "end": start + 4}])[0]
    assert 0 < len(got) <= 2 * limit + 16
    assert got.startswith("word") and got.endswith("word")  # 不切在半个词上
    assert "  " not in got


def test_out_of_range_and_empty_inputs_do_not_explode(js):
    got = js(
        [
            {"fn": "slice", "text": "", "start": 0, "end": 5},
            {"fn": "slice", "text": "Only one sentence here.", "start": -20, "end": 999},
            {"fn": "slice", "text": "Backwards offsets.", "start": 10, "end": 2},
        ]
    )
    assert got[0] == ""
    assert got[1] == "Only one sentence here."
    assert got[2] == "Backwards offsets."


# --- 选区筛子（决定 ⌖ 浮标出不出来） ---------------------------------------


def test_looks_english_gate(js):
    got = js(
        [
            {"fn": "english", "text": "stakeout"},
            {"fn": "english", "text": "  Cold  Feet "},
            {"fn": "english", "text": "hire a new driver"},
            {"fn": "english", "text": "汉字选区"},
            {"fn": "english", "text": "12345 67"},
            {"fn": "english", "text": ""},
            {"fn": "english", "text": "one two three four five six seven"},
            {"fn": "english", "text": "x" * 65},
        ]
    )
    assert got == [True, True, True, False, False, False, False, False]
