"""pytest 夹具。

版权红线（DESIGN §0 §6）：所有字幕夹具都是自造英文句子，
不含任何真实剧集台词。srt 内容写在代码里、跑测试时落到 tmp_path，
既避开 .gitignore 的 *.srt 规则，也保证仓库里不出现 .srt 文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db  # noqa: E402

# 自造台词：覆盖多行 cue、缩写、重复词、专名、连字符、不规则变形
FIXTURE_SRT = """1
00:00:01,000 --> 00:00:03,500
The tall gardener went home early.

2
00:00:03,500 --> 00:00:07,250
Marlow says it's raining again,
and I don't believe her at all.

3
00:00:08,000 --> 00:00:11,000
My cousins bought two cameras; the cameras were cheap.

4
00:00:12,125 --> 00:00:15,000
Ex-con Bramwell left Halloway Street 3 days ago.

5
00:00:16,000 --> 00:00:18,000
"Stop!" she shouted -- nobody stopped.
"""

# 第二版：段数变少 + 文本改动，用来测重复 ingest 的收敛行为
FIXTURE_SRT_SHORTER = """1
00:00:01,000 --> 00:00:03,500
The tall gardener went home early.

2
00:00:03,500 --> 00:00:06,000
A quiet morning in the empty office.
"""

# 格式脏活：BOM、CRLF、缺序号、点号毫秒、多余空行、格式标签
FIXTURE_SRT_MESSY = (
    "﻿1\r\n"
    "00:00:01.000 --> 00:00:02.000\r\n"
    "<i>Hello there</i>\r\n"
    "\r\n"
    "\r\n"
    "00:00:02,000 --> 00:00:04,000\r\n"
    "{\\an8}Second   cue with   spaces\r\n"
    "second line\r\n"
)


@pytest.fixture()
def srt_file(tmp_path: Path) -> Path:
    p = tmp_path / "fixture.srt"
    p.write_text(FIXTURE_SRT, encoding="utf-8")
    return p


@pytest.fixture()
def srt_file_shorter(tmp_path: Path) -> Path:
    p = tmp_path / "fixture_shorter.srt"
    p.write_text(FIXTURE_SRT_SHORTER, encoding="utf-8")
    return p


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def conn(db_path: Path):
    c = init_db(db_path)
    yield c
    c.close()
