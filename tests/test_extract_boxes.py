"""extract_hardsub 词级包围盒（DESIGN §4 热区）。

版权红线（DESIGN §0 §6）：所有 OCR 夹具都是自造英文句子 + 合成图片，
不含任何真实剧集台词或截图。

分层：
  - 纯逻辑用例用合成 TSV 文本，不跑 tesseract（坐标换算 / 清洗对齐 / 合并丢弃）
  - @pytest.mark.slow 用例渲染一张白底黑字假字幕条，真跑系统 tesseract
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import extract_hardsub as EH  # noqa: E402

CROP = (1920, 54, 0, 1026)          # 与默认 --crop 1920:54:0:1026 一致
TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext"
)


def tsv(*words, header=True):
    """合成 tesseract TSV：words 是 (left, top, w, h, conf, text) 元组。"""
    lines = [TSV_HEADER] if header else []
    lines.append("1\t1\t0\t0\t0\t0\t0\t0\t3840\t108\t-1\t")          # page
    lines.append("2\t1\t1\t0\t0\t0\t0\t0\t3840\t108\t-1\t")          # block
    lines.append("3\t1\t1\t1\t0\t0\t0\t0\t3840\t108\t-1\t")          # para
    lines.append("4\t1\t1\t1\t1\t0\t0\t0\t3840\t108\t-1\t")          # line
    for i, (left, top, w, h, conf, text) in enumerate(words, 1):
        lines.append(
            f"5\t1\t1\t1\t1\t{i}\t{left}\t{top}\t{w}\t{h}\t{conf}\t{text}"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- parse_crop

def test_parse_crop_full():
    assert EH.parse_crop("1920:54:0:1026") == (1920, 54, 0, 1026)


def test_parse_crop_defaults_offset():
    assert EH.parse_crop("640:48") == (640, 48, 0, 0)


@pytest.mark.parametrize("bad", ["1920:54:0", "iw:54:0:1026", "1920:0:0:0", ""])
def test_parse_crop_rejects_garbage(bad):
    with pytest.raises(ValueError):
        EH.parse_crop(bad)


# ------------------------------------------------------------ 坐标换算

def test_boxes_are_halved_and_offset_by_crop():
    """TSV 坐标在 2x 放大的 crop 条里；输出须回到 1920x1080 原始帧坐标。"""
    words, boxes = EH.parse_tsv(
        tsv((200, 20, 100, 60, 95.0, "hello"),
            (400, 24, 80, 56, 90.0, "there")), CROP)
    assert words == ["hello", "there"]
    assert boxes[0] == {"x": 100, "y": 1036, "width": 50, "height": 30}
    assert boxes[1] == {"x": 200, "y": 1038, "width": 40, "height": 28}


def test_crop_x_offset_is_applied():
    words, boxes = EH.parse_tsv(
        tsv((100, 10, 40, 40, 88.0, "word")), (1000, 54, 460, 1026))
    assert words == ["word"]
    assert boxes[0]["x"] == 50 + 460
    assert boxes[0]["y"] == 5 + 1026


def test_boxes_stay_inside_the_full_frame():
    _, boxes = EH.parse_tsv(
        tsv((3838, 100, 2, 8, 90.0, "x")), CROP)          # 贴着 crop 右下角
    b = boxes[0]
    assert 0 <= b["x"] and b["x"] + b["width"] <= 1920
    assert 1026 <= b["y"] and b["y"] + b["height"] <= 1080


def test_no_crop_geometry_means_no_boxes():
    words, boxes = EH.parse_tsv(tsv((200, 20, 100, 60, 95.0, "hello")), None)
    assert words == ["hello"] and boxes == [None]


# ------------------------------------------------------- 丢框不丢词

def test_low_confidence_drops_box_keeps_word():
    words, boxes = EH.parse_tsv(
        tsv((200, 20, 100, 60, 95.0, "solid"),
            (400, 20, 100, 60, 4.0, "shaky")), CROP)
    assert words == ["solid", "shaky"]
    assert boxes[0] is not None and boxes[1] is None


def test_zero_width_or_height_drops_box():
    _, boxes = EH.parse_tsv(
        tsv((200, 20, 0, 60, 95.0, "flat"),
            (300, 20, 40, 0, 95.0, "thin"),
            (400, 20, 1, 1, 95.0, "tiny")), CROP)
    assert boxes[0] is None and boxes[1] is None
    assert boxes[2] is None          # 半像素框取整后退化 -> 丢


def test_box_outside_the_crop_band_is_dropped():
    _, boxes = EH.parse_tsv(
        tsv((3800, 20, 200, 40, 95.0, "past-right"),
            (100, 90, 40, 40, 95.0, "past-bottom")), CROP)
    assert boxes == [None, None]


def test_non_word_rows_and_blanks_are_ignored():
    body = tsv((200, 20, 100, 60, 95.0, "only"))
    body += "5\t1\t1\t1\t1\t9\t0\t0\t10\t10\t-1\t \n"     # 空白 word 行
    words, boxes = EH.parse_tsv(body, CROP)
    assert words == ["only"] and len(boxes) == 1


def test_empty_tsv_yields_nothing():
    assert EH.parse_tsv(TSV_HEADER + "\n", CROP) == ([], [])


# ------------------------------------------------- clean() 与词框对齐

def test_words_align_when_clean_only_touches_characters():
    raw = "| think it's fine"
    boxes = [{"x": 10 * i, "y": 1030, "width": 8, "height": 20} for i in range(4)]
    out = EH.words_with_boxes(raw, boxes, EH.clean(raw))
    assert [w["w"] for w in out] == ["I", "think", "it's", "fine"]
    assert [w["x"] for w in out] == [0, 10, 20, 30]


def test_lowercased_is_keeps_its_own_box():
    raw = "the door Is open"
    boxes = [{"x": 100 + 50 * i, "y": 1030, "width": 40, "height": 20} for i in range(4)]
    out = EH.words_with_boxes(raw, boxes, EH.clean(raw))
    assert [w["w"] for w in out] == ["the", "door", "is", "open"]
    assert [w["x"] for w in out] == [100, 150, 200, 250]


def test_merged_pipes_merge_their_boxes():
    """'| |' -> 'I'：两个框合并成一个，词序不错位。"""
    raw = "| | just left"
    boxes = [
        {"x": 100, "y": 1030, "width": 6, "height": 30},
        {"x": 110, "y": 1032, "width": 6, "height": 28},
        {"x": 130, "y": 1030, "width": 40, "height": 30},
        {"x": 180, "y": 1030, "width": 40, "height": 30},
    ]
    out = EH.words_with_boxes(raw, boxes, EH.clean(raw))
    assert [w["w"] for w in out] == ["I", "just", "left"]
    assert out[0] == {"w": "I", "x": 100, "y": 1030, "width": 16, "height": 30}
    assert out[1]["x"] == 130 and out[2]["x"] == 180


def test_absurd_merge_is_dropped_not_kept_huge():
    """一半在画面左边缘的噪点 + 真字母：合并框会吞掉邻词 -> 宁可无框。"""
    raw = "| | just left"
    boxes = [
        {"x": 0, "y": 1038, "width": 73, "height": 36},      # 光斑噪点
        {"x": 676, "y": 1031, "width": 4, "height": 28},     # 真正的 I
        {"x": 691, "y": 1029, "width": 68, "height": 39},
        {"x": 771, "y": 1033, "width": 57, "height": 35},
    ]
    out = EH.words_with_boxes(raw, boxes, EH.clean(raw))
    assert [w["w"] for w in out] == ["I", "just", "left"]
    assert out[0]["x"] is None and out[0]["width"] is None
    assert out[1]["x"] == 691                     # 后续词不受影响


def test_merge_of_missing_boxes_yields_no_box():
    raw = "| | ok"
    boxes = [None, None, {"x": 50, "y": 1030, "width": 20, "height": 20}]
    out = EH.words_with_boxes(raw, boxes, EH.clean(raw))
    assert [w["w"] for w in out] == ["I", "ok"]
    assert out[0]["x"] is None and out[1]["x"] == 50


def test_word_without_box_uses_null_coordinates():
    raw = "keep going"
    out = EH.words_with_boxes(raw, [None, {"x": 5, "y": 1030, "width": 9, "height": 9}],
                              EH.clean(raw))
    assert out[0] == {"w": "keep", "x": None, "y": None, "width": None, "height": None}
    assert out[1]["w"] == "going" and out[1]["x"] == 5


def test_box_count_desync_degrades_to_no_boxes():
    """框数与词数对不上时（理论上不该发生）：全部无框，绝不错位。"""
    out = EH.words_with_boxes("one two three", [{"x": 1, "y": 2, "width": 3, "height": 4}],
                              "one two three")
    assert [w["w"] for w in out] == ["one", "two", "three"]
    assert all(w["x"] is None for w in out)


def test_words_always_match_cleaned_text_tokens():
    raw = "| | said the door Is | open"
    boxes = [{"x": 10 * i, "y": 1030, "width": 8, "height": 20} for i in range(8)]
    cleaned = EH.clean(raw)
    out = EH.words_with_boxes(raw, boxes, cleaned)
    assert [w["w"] for w in out] == cleaned.split()


# ----------------------------------------------------------- boxes json

def test_boxes_payload_shape_matches_contract():
    cues = [{
        "start": 1.0, "end": 2.5, "text": "I left",
        "words": [{"w": "I", "x": 10, "y": 1030, "width": 6, "height": 20},
                  {"w": "left", "x": 20, "y": 1030, "width": 40, "height": 20}],
    }]
    payload = EH.boxes_payload(cues)
    assert payload[0]["idx"] == 1                        # 与 srt 序号（1 起）一致
    assert payload[0]["start"] == 1.0 and payload[0]["end"] == 2.5
    assert payload[0]["text"] == "I left"
    assert [w["w"] for w in payload[0]["words"]] == ["I", "left"]
    for w in payload[0]["words"]:
        assert all(isinstance(w[k], int) for k in ("x", "y", "width", "height"))


def test_write_boxes_json_is_utf8_and_loadable(tmp_path):
    cues = [{"start": 0.0, "end": 1.0, "text": "café now",
             "words": [{"w": "café", "x": 1, "y": 1026, "width": 2, "height": 3},
                       {"w": "now", "x": None, "y": None, "width": None, "height": None}]}]
    p = tmp_path / "b.json"
    EH.write_boxes_json(cues, p)
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got[0]["words"][0]["w"] == "café"
    assert got[0]["words"][1]["x"] is None


# ------------------------------------------------------------ end-to-end

pytestmark_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="需要系统 tesseract")


def _fake_strip(path, text="The quiet gardener waited outside", width=900, height=54):
    """白底黑字反过来：合成一条 crop 条（白字黑底，同真实硬字幕条）。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(img)
    font = None
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if Path(cand).exists():
            font = ImageFont.truetype(cand, 30)
            break
    d.text((40, 10), text, fill=255, font=font)
    img.save(path)
    return text


@pytest.mark.slow
@pytestmark_tesseract
def test_ocr_frame_returns_text_and_aligned_boxes(tmp_path):
    png = tmp_path / "strip.png"
    text = _fake_strip(png)
    crop = (900, 54, 60, 1026)
    got, boxes = EH.ocr_frame(str(png), crop)
    assert got == text                                   # 合成字幕条应逐词认对
    assert len(boxes) == len(got.split())
    for w, b in zip(got.split(), boxes):
        assert b is not None, w
        assert 60 <= b["x"] < 60 + 900
        assert 1026 <= b["y"] < 1026 + 54
        assert b["width"] > 0 and b["height"] > 0
    xs = [b["x"] for b in boxes]
    assert xs == sorted(xs)                              # 左到右单调
    assert not (tmp_path / "strip.png.bw.png").exists()  # 临时文件已清理


@pytest.mark.slow
@pytestmark_tesseract
def test_ocr_frame_without_crop_keeps_text_but_no_boxes(tmp_path):
    png = tmp_path / "strip.png"
    text = _fake_strip(png)
    got, boxes = EH.ocr_frame(str(png))
    assert got == text
    assert boxes == [None] * len(text.split())


@pytest.mark.slow
@pytestmark_tesseract
def test_tsv_text_matches_plain_psm7_text(tmp_path):
    """红线保障：TSV 拼回来的文本 == 旧版 --psm 7 纯文本输出。"""
    import numpy as np
    from PIL import Image

    png = tmp_path / "strip.png"
    _fake_strip(png)
    a = np.asarray(Image.open(png).convert("L"), dtype=np.uint8)
    bw = Image.fromarray(np.where(a > EH.WHITE_THRESHOLD, 0, 255).astype(np.uint8))
    bw = bw.resize((bw.width * EH.UPSCALE, bw.height * EH.UPSCALE), Image.LANCZOS)
    bwp = tmp_path / "strip.bw.png"
    bw.save(bwp)
    plain = subprocess.run(
        ["tesseract", str(bwp), "stdout", "-l", "eng", "--psm", "7"],
        capture_output=True, text=True).stdout
    tsv_text, _ = EH.ocr_frame(str(png), (900, 54, 0, 1026))
    assert tsv_text == " ".join(plain.split())
