"""srt 解析：时间戳、多行 cue、脏格式。"""

from __future__ import annotations

import pytest

from app.ingest import parse_srt, parse_srt_file, parse_timestamp
from tests.conftest import FIXTURE_SRT_MESSY


def test_parse_timestamp_to_seconds():
    start, end = parse_timestamp("00:00:12,333 --> 00:01:14,667")
    assert start == pytest.approx(12.333)
    assert end == pytest.approx(74.667)


def test_parse_timestamp_hours_and_dot_separator():
    start, end = parse_timestamp("01:02:03.500 --> 01:02:04.000")
    assert start == pytest.approx(3723.5)
    assert end == pytest.approx(3724.0)


def test_parse_timestamp_rejects_garbage():
    with pytest.raises(ValueError):
        parse_timestamp("not a timestamp")


def test_parse_srt_counts_and_order(srt_file):
    cues = parse_srt_file(srt_file)
    assert len(cues) == 5
    assert [c.idx for c in cues] == [1, 2, 3, 4, 5]
    assert cues[0].t_start == pytest.approx(1.0)
    assert cues[0].t_end == pytest.approx(3.5)
    assert cues[3].t_start == pytest.approx(12.125)


def test_multiline_cue_preserves_linebreak(srt_file):
    cues = parse_srt_file(srt_file)
    two_line = cues[1]
    assert "\n" in two_line.text
    assert two_line.text.startswith("Marlow says it's raining again,")
    assert two_line.text.endswith("and I don't believe her at all.")


def test_parse_srt_messy_input():
    cues = parse_srt(FIXTURE_SRT_MESSY)
    assert len(cues) == 2
    # BOM 去掉、<i> 标签剥掉
    assert cues[0].text == "Hello there"
    # 缺序号的块照样收；{\an8} 剥掉；多空格压成一个；多行保留换行
    assert cues[1].idx == 2
    assert cues[1].text == "Second cue with spaces\nsecond line"
    assert cues[1].t_start == pytest.approx(2.0)


def test_parse_srt_drops_blocks_without_timestamp():
    text = "1\n00:00:01,000 --> 00:00:02,000\nreal line\n\n2\njunk block\n"
    cues = parse_srt(text)
    assert len(cues) == 1
    assert cues[0].text == "real line"


def test_parse_srt_drops_empty_text_blocks():
    text = "1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n00:00:03,000 --> 00:00:04,000\nkept\n"
    cues = parse_srt(text)
    assert [c.text for c in cues] == ["kept"]
