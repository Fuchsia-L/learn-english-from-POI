"""scripts/build_cet46.py：抽词/合并/去重/lemma 归一 + clone 失败提示。

夹具是**自造的 mini 词表**（模仿上游格式，内容自己编的），
不联网：clone 路径一律 monkeypatch 掉 subprocess.run。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_cet46 as B  # noqa: E402

# 自造四级样表：带标题行、计数行、字母分节头、词条、粘连的音标括号、括号可选后缀
MINI_CET4 = """﻿大学英语四级大纲单词表
(共 8 词)

A

a art.一(个)；每一(个)
abandon [əˈbændən] vt.丢弃；放弃
able [ˈeibl] a.有能力的
cameras [ˈkæmərə] n.照相机（复数形式，用来验 lemma 归一）

B

B.C. (缩)公元前
bought [bɔːt] v.买（buy 的过去式）
buy [bai] vt.买
instruct[ inˈstrʌkt] vt.教；指示
toward(s) [təˈwɔːd] prep.向；对于
"""

# 自造六级样表：与四级有重叠（able / buy），也有新词
MINI_CET6 = """able [ˈeibl] adj. 有能力的
buy [bai] v. 买
stakeout [ˈsteɪkaʊt] n. 盯梢，监视
oˈclock [əˈklɔk] ad. …点钟
"""


@pytest.fixture()
def sources(tmp_path: Path) -> list[Path]:
    c4 = tmp_path / "MINI_CET4.txt"
    c6 = tmp_path / "MINI_CET6.txt"
    c4.write_text(MINI_CET4, encoding="utf-8")
    c6.write_text(MINI_CET6, encoding="utf-8")
    return [c4, c6]


# --- 抽词 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expect",
    [
        ("abandon [əˈbændən] vt.丢弃；放弃", "abandon"),
        ("instruct[ inˈstrʌkt] vt.教", "instruct"),  # 上游少个空格，照样吃
        ("toward(s) [təˈwɔːd] prep.向", "toward"),  # 可选后缀不进词表
        ("systematic(al) [ˌsistiˈmætik] a.有系统的", "systematic"),
        ("oˈclock [əˈklɔk] ad.…点钟", "o'clock"),  # 修饰符撇号归一成 ASCII
        ("Abandon", "abandon"),  # 一律小写
        ("  able [ˈeibl] a.有能力的  ", "able"),
        ("a art.一(个)", "a"),  # 单字母真词保留
        ("I pron.我", "i"),
        ("buzz word [bʌz wɜːd] n.专业词语", "buzz"),  # 词组只取头词
        ("B", None),  # 字母分节头
        ("B.C. (缩)公元前", None),  # 缩写头字母，同样丢
        ("大学英语四级大纲单词表", None),
        ("(共 4615 词)", None),
        ("", None),
        ("   ", None),
        ("# 注释", None),
        ("123 数字开头", None),
    ],
)
def test_extract_word(line, expect):
    assert B.extract_word(line) == expect


def test_parse_wordlist_dedupes_and_keeps_order():
    words = B.parse_wordlist(["buy [bai] vt.买", "able a.", "buy v.买", "Able adj."])
    assert words == ["buy", "able"]


def test_read_source_handles_bom_and_headers(sources):
    words = B.read_source(sources[0])
    assert words == [
        "a", "abandon", "able", "cameras", "bought", "buy", "instruct", "toward"
    ]


# --- 合并 / 归一 -----------------------------------------------------------


def test_merge_lemmatizes_and_dedupes(sources):
    merged = B.merge_sources([(p.name, B.read_source(p)) for p in sources])
    # cameras → camera、bought → buy（与 buy 撞车后去重）
    assert merged["words"] == [
        "a", "abandon", "able", "buy", "camera", "instruct", "o'clock", "stakeout",
        "toward",
    ]
    c4, c6 = merged["per_source"]
    assert c4["name"] == "MINI_CET4.txt"
    assert c4["words"] == 8 and c4["lemmas"] == 7  # bought/buy 合成一个
    assert c4["changed_by_lemma"] == 2  # cameras→camera, bought→buy
    assert c4["added"] == 7
    # 六级只算增量：able / buy 已在四级里
    assert c6["words"] == 4 and c6["added"] == 2
    assert set(merged["words"]) & {"bought", "cameras"} == set()


def test_merge_is_order_independent_for_the_final_set(sources):
    a = B.merge_sources([(p.name, B.read_source(p)) for p in sources])
    b = B.merge_sources([(p.name, B.read_source(p)) for p in reversed(sources)])
    assert a["words"] == b["words"]  # 集合一样，只有 added 增量口径依赖顺序


# --- 输出 ------------------------------------------------------------------


def test_write_wordlist_roundtrips_through_prefetch_loader(tmp_path, sources):
    from prefetch import load_wordlist

    out = tmp_path / "sub" / "cet46.txt"
    merged = B.merge_sources([(p.name, B.read_source(p)) for p in sources])
    n = B.write_wordlist(out, merged["words"], "自造 mini 夹具")
    assert n == len(merged["words"])
    body = out.read_text(encoding="utf-8").splitlines()
    assert body[0].startswith("#") and body[1].startswith("#")
    assert body[2:] == merged["words"]  # 注释之后一行一词
    assert load_wordlist(out) == set(merged["words"])  # prefetch 吃得下


def test_build_with_local_sources(tmp_path, sources):
    out = tmp_path / "cet46.txt"
    stats = B.build(out_path=out, sources=sources, work_dir=tmp_path / "clone")
    assert stats["total"] == stats["written"] == 9
    assert "本地文件" in stats["source"]
    assert out.is_file() and not (tmp_path / "clone").exists()


def test_build_dry_run_writes_nothing(tmp_path, sources):
    out = tmp_path / "cet46.txt"
    stats = B.build(out_path=out, sources=sources, work_dir=tmp_path / "c", dry_run=True)
    assert stats["dry_run"] is True and stats["written"] == 0 and not out.exists()


def test_build_rejects_missing_source(tmp_path):
    with pytest.raises(RuntimeError, match="不存在"):
        B.build(tmp_path / "o.txt", [tmp_path / "nope.txt"], tmp_path / "c")


def test_main_with_sources(tmp_path, sources, capsys):
    out = tmp_path / "cet46.txt"
    rc = B.main(["-o", str(out), *sum([["--source", str(p)] for p in sources], [])])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "合计唯一 lemma" in printed and "MINI_CET4.txt" in printed
    assert out.is_file()


# --- clone 路径（全程 mock，不联网） ---------------------------------------


def test_clone_failure_gives_retry_hint(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output=True, text=True):
        assert cmd[:3] == ["git", "clone", "--depth"]
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: unable to access")

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        B.clone_wordlists(tmp_path / "clone")
    msg = str(exc.value)
    assert "git clone 失败" in msg and "fatal: unable to access" in msg
    assert "--source" in msg and B.WORDLIST_REPO in msg


def test_main_returns_2_when_clone_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        B.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "网络不通"),
    )
    rc = B.main(["-o", str(tmp_path / "cet46.txt"), "--work-dir", str(tmp_path / "c")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[fail]" in err and "[hint]" in err
    assert not (tmp_path / "cet46.txt").exists()


def test_clone_missing_expected_files(tmp_path, monkeypatch):
    work = tmp_path / "clone"

    def fake_run(cmd, capture_output=True, text=True):
        work.mkdir(parents=True, exist_ok=True)
        (work / "README.md").write_text("上游改结构了", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="缺文件"):
        B.clone_wordlists(work)


def test_build_cleans_clone_dir(tmp_path, monkeypatch, sources):
    work = tmp_path / "clone"

    def fake_run(cmd, capture_output=True, text=True):
        work.mkdir(parents=True, exist_ok=True)
        for name, src in zip(B.SOURCE_NAMES, sources):
            (work / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    out = tmp_path / "cet46.txt"
    stats = B.build(out_path=out, sources=None, work_dir=work)
    assert stats["total"] == 9 and stats["source"] == B.WORDLIST_REPO
    assert not work.exists()  # 用完即删
    assert out.is_file()


def test_build_keeps_clone_when_asked(tmp_path, monkeypatch, sources):
    work = tmp_path / "clone"

    def fake_run(cmd, capture_output=True, text=True):
        work.mkdir(parents=True, exist_ok=True)
        for name, src in zip(B.SOURCE_NAMES, sources):
            (work / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    B.build(tmp_path / "cet46.txt", None, work, keep_clone=True)
    assert (work / B.SOURCE_NAMES[0]).is_file()
