"""build_ecdict 的离线路径：mini 夹具可构建、可查、schema 正确。

不测网络克隆（CI/离线环境不可靠）；克隆路径靠 --csv 分支覆盖解析逻辑。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_ecdict  # noqa: E402


def test_mini_dict_has_100_entries(tmp_path):
    out = tmp_path / "ecdict_mini.db"
    total = build_ecdict.build_mini(out)
    assert total == 100
    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) c FROM ecdict").fetchone()["c"] == 100
    assert (
        conn.execute("SELECT value FROM meta WHERE key='entries'").fetchone()["value"]
        == "100"
    )
    conn.close()


def test_mini_dict_lookup_by_lowercase(tmp_path):
    out = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(out)
    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM ecdict WHERE word_lower = ?", ("stakeout",)
    ).fetchone()
    assert row["phonetic"] and row["translation"]
    assert row["word"] == "stakeout"
    # 索引存在，lookup 不做全表扫
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM ecdict WHERE word_lower = 'go'"
    ).fetchall()
    assert any("idx_ecdict_word_lower" in str(tuple(r)) for r in plan)
    conn.close()


def test_csv_path_builds_without_network(tmp_path):
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text(
        "word,phonetic,definition,translation,pos,collins,oxford,tag,bnc,frq,exchange,detail,audio\n"
        "Gadget,'ɡædʒɪt,a small device,n. 小装置,n:100,3,1,cet6,4000,3500,s:gadgets,,\n"
        "gadget,'ɡædʒɪt,,n. 小工具,,,,,0,0,,,\n",
        encoding="utf-8",
    )
    out = tmp_path / "ecdict.db"
    total = build_ecdict.build(out, csv_path, tmp_path / "unused_clone", keep_clone=False)
    assert total == 2
    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ecdict WHERE word_lower = 'gadget' "
        "ORDER BY (word = word_lower) DESC"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["word"] == "gadget"
    upper = [r for r in rows if r["word"] == "Gadget"][0]
    assert upper["collins"] == 3 and upper["tag"] == "cet6" and upper["frq"] == 3500
    assert upper["exchange"] == "s:gadgets"
    conn.close()


def test_mini_covers_fixture_vocabulary(tmp_path):
    """mini 词典要覆盖测试夹具里的常见词，才能给 /lookup 冒烟用。"""
    out = tmp_path / "ecdict_mini.db"
    build_ecdict.build_mini(out)
    conn = sqlite3.connect(str(out))
    words = {r[0] for r in conn.execute("SELECT word_lower FROM ecdict")}
    assert {"go", "cousin", "check", "job", "work", "stakeout"} <= words
    conn.close()
