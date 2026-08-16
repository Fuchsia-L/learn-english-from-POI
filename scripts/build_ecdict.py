#!/usr/bin/env python3
"""构建本地词典 data/ecdict.db（DESIGN.md §1 §6 build_ecdict 行）。

数据源：GitHub 公开仓库 skywind3000/ECDICT 的 ecdict.csv（CC-BY / MIT，见其 LICENSE）。
走 `git clone --depth 1`（raw.githubusercontent.com 在本机可能被代理拦，不依赖它）。
克隆约 190MB，转换完成后默认删除克隆目录。

用法:
    python scripts/build_ecdict.py                      # 克隆 → 构建 → 清理
    python scripts/build_ecdict.py --csv /path/ecdict.csv   # 用已下载的 csv（可重试）
    python scripts/build_ecdict.py --keep-clone --work-dir /tmp/ecdict_src
    python scripts/build_ecdict.py --mini -o data/ecdict_mini.db   # 离线 mini 夹具

网络不通时脚本以非 0 退出并打印失败原因 + 重试方式；--mini 永远可离线跑，
产出 100 词的自造 mini 词典供测试使用（内容自造，不含 ECDICT 数据）。
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ECDICT_REPO = "https://github.com/skywind3000/ECDICT.git"
CSV_NAME = "ecdict.csv"

# ECDICT csv 表头 → 我们保留的列
COLUMNS = (
    "word",
    "phonetic",
    "definition",
    "translation",
    "pos",
    "collins",
    "oxford",
    "tag",
    "bnc",
    "frq",
    "exchange",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ecdict (
    id          INTEGER PRIMARY KEY,
    word        TEXT NOT NULL,
    word_lower  TEXT NOT NULL,
    phonetic    TEXT,   -- 音标
    definition  TEXT,   -- 英文释义
    translation TEXT,   -- 中文释义
    pos         TEXT,   -- 词性占比，如 n:53/v:47
    collins     INTEGER,-- 柯林斯星级 1-5
    oxford      INTEGER,-- 是否牛津三千核心词
    tag         TEXT,   -- 考试标签 zk/gk/cet4/cet6/ky/toefl/ielts/gre
    bnc         INTEGER,-- BNC 词频顺位
    frq         INTEGER,-- 当代语料词频顺位
    exchange    TEXT    -- 时态/单复数变换，含 0:词根
);
CREATE INDEX IF NOT EXISTS idx_ecdict_word_lower ON ecdict (word_lower);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# --- mini 夹具：100 条自造词典条目（音标/释义自写，非 ECDICT 数据） ----------
# 格式: word|phonetic|translation|pos|tag|collins
MINI_ROWS_RAW = """
a|ə|art. 一个|art.|zk|5
about|əˈbaʊt|prep. 关于；adv. 大约|prep.|zk|5
all|ɔːl|adj. 全部的；pron. 全部|adj.|zk|5
and|ænd|conj. 和|conj.|zk|5
answer|ˈɑːnsə|n. 回答；v. 回答|n.|zk|5
ask|ɑːsk|v. 问；请求|v.|zk|5
back|bæk|n. 背部；adv. 向后|n.|zk|5
bad|bæd|adj. 坏的；糟糕的|adj.|zk|5
be|biː|v. 是；存在|v.|zk|5
begin|bɪˈɡɪn|v. 开始|v.|zk|4
book|bʊk|n. 书；v. 预订|n.|zk|5
call|kɔːl|v. 呼叫；打电话|v.|zk|5
car|kɑː|n. 汽车|n.|zk|5
check|tʃek|n. 支票；v. 检查|v.|cet4|4
child|tʃaɪld|n. 孩子|n.|zk|5
city|ˈsɪti|n. 城市|n.|zk|5
come|kʌm|v. 来|v.|zk|5
cop|kɒp|n. 警察（口语）|n.|cet6|2
cousin|ˈkʌzn|n. 堂表兄弟姐妹|n.|zk|3
day|deɪ|n. 天；白天|n.|zk|5
do|duː|v. 做|v.|zk|5
door|dɔː|n. 门|n.|zk|5
drive|draɪv|v. 驾驶；n. 驱动|v.|zk|4
eat|iːt|v. 吃|v.|zk|5
experiment|ɪkˈsperɪmənt|n. 实验；v. 做实验|n.|cet4|4
eye|aɪ|n. 眼睛|n.|zk|5
face|feɪs|n. 脸；v. 面对|n.|zk|5
family|ˈfæməli|n. 家庭|n.|zk|5
find|faɪnd|v. 找到|v.|zk|5
foodie|ˈfuːdi|n. 美食爱好者|n.||1
friend|frend|n. 朋友|n.|zk|5
get|ɡet|v. 得到；变得|v.|zk|5
give|ɡɪv|v. 给|v.|zk|5
go|ɡəʊ|v. 去|v.|zk|5
good|ɡʊd|adj. 好的|adj.|zk|5
hand|hænd|n. 手|n.|zk|5
happy|ˈhæpi|adj. 高兴的|adj.|zk|5
have|hæv|v. 有|v.|zk|5
head|hed|n. 头；v. 领导|n.|zk|5
hear|hɪə|v. 听见|v.|zk|5
help|help|v. 帮助|v.|zk|5
here|hɪə|adv. 这里|adv.|zk|5
hire|ˈhaɪə|v. 雇用；租用|v.|cet4|3
home|həʊm|n. 家|n.|zk|5
hour|ˈaʊə|n. 小时|n.|zk|5
house|haʊs|n. 房子|n.|zk|5
job|dʒɒb|n. 工作|n.|zk|5
join|dʒɔɪn|v. 加入；连接|v.|zk|4
keep|kiːp|v. 保持|v.|zk|5
kill|kɪl|v. 杀死|v.|zk|4
know|nəʊ|v. 知道|v.|zk|5
learn|lɜːn|v. 学习|v.|zk|5
leave|liːv|v. 离开；n. 休假|v.|zk|5
life|laɪf|n. 生命；生活|n.|zk|5
like|laɪk|v. 喜欢；prep. 像|v.|zk|5
listen|ˈlɪsn|v. 听|v.|zk|4
long|lɒŋ|adj. 长的|adj.|zk|5
look|lʊk|v. 看|v.|zk|5
lot|lɒt|n. 许多；一块地|n.|zk|4
love|lʌv|v. 爱；n. 爱情|v.|zk|5
make|meɪk|v. 制作；使得|v.|zk|5
man|mæn|n. 男人|n.|zk|5
mean|miːn|v. 意味着；adj. 吝啬的|v.|zk|5
mind|maɪnd|n. 思想；v. 介意|n.|zk|4
name|neɪm|n. 名字|n.|zk|5
new|njuː|adj. 新的|adj.|zk|5
night|naɪt|n. 夜晚|n.|zk|5
number|ˈnʌmbə|n. 数字；号码|n.|zk|5
okay|ˌəʊˈkeɪ|adj. 好的；adv. 好吧|adj.||3
open|ˈəʊpən|v. 打开；adj. 开着的|v.|zk|5
pay|peɪ|n. 报酬；v. 支付|v.|zk|5
people|ˈpiːpl|n. 人们|n.|zk|5
phone|fəʊn|n. 电话|n.|zk|5
place|pleɪs|n. 地方|n.|zk|5
play|pleɪ|v. 玩；演奏|v.|zk|5
put|pʊt|v. 放|v.|zk|5
rain|reɪn|n. 雨；v. 下雨|n.|zk|4
read|riːd|v. 读|v.|zk|5
run|rʌn|v. 跑；经营|v.|zk|5
say|seɪ|v. 说|v.|zk|5
see|siː|v. 看见|v.|zk|5
sorry|ˈsɒri|adj. 抱歉的|adj.|zk|4
stakeout|ˈsteɪkaʊt|n. 盯梢；监视|n.||1
take|teɪk|v. 拿；花费|v.|zk|5
talk|tɔːk|v. 谈话|v.|zk|5
tell|tel|v. 告诉|v.|zk|5
thing|θɪŋ|n. 事情；东西|n.|zk|5
think|θɪŋk|v. 想；认为|v.|zk|5
time|taɪm|n. 时间|n.|zk|5
understand|ˌʌndəˈstænd|v. 理解|v.|zk|5
wait|weɪt|v. 等待|v.|zk|4
walk|wɔːk|v. 走路|v.|zk|5
want|wɒnt|v. 想要|v.|zk|5
watch|wɒtʃ|v. 观看；n. 手表|v.|zk|5
water|ˈwɔːtə|n. 水|n.|zk|5
way|weɪ|n. 方式；路|n.|zk|5
week|wiːk|n. 周|n.|zk|5
wife|waɪf|n. 妻子|n.|zk|4
word|wɜːd|n. 单词|n.|zk|5
work|wɜːk|n. 工作；v. 工作|n.|zk|5
"""


def mini_rows() -> list[dict]:
    rows = []
    for line in MINI_ROWS_RAW.strip().splitlines():
        word, phonetic, translation, pos, tag, collins = line.split("|")
        rows.append(
            {
                "word": word,
                "phonetic": phonetic,
                "definition": "",
                "translation": translation,
                "pos": pos,
                "collins": collins,
                "oxford": "",
                "tag": tag,
                "bnc": "",
                "frq": "",
                "exchange": "",
            }
        )
    return rows


# --- 构建 ------------------------------------------------------------------


def _clean_text(v: str | None):
    """去掉 ECDICT 里残留的 CR 伪影；释义内的 '\\n' 分隔符保持原样（前端按它折行）。"""
    if not v:
        return None
    s = str(v).replace("\\r", "").replace("\r", "").strip()
    return s or None


def _to_int(v: str | None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def create_db(out_path: Path) -> sqlite3.Connection:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    return conn


def write_rows(conn: sqlite3.Connection, rows, source: str) -> int:
    sql = (
        "INSERT INTO ecdict "
        "(word, word_lower, phonetic, definition, translation, pos, collins,"
        " oxford, tag, bnc, frq, exchange) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    batch, total = [], 0
    for row in rows:
        word = (row.get("word") or "").strip()
        if not word:
            continue
        batch.append(
            (
                word,
                word.lower(),
                _clean_text(row.get("phonetic")),
                _clean_text(row.get("definition")),
                _clean_text(row.get("translation")),
                _clean_text(row.get("pos")),
                _to_int(row.get("collins")),
                _to_int(row.get("oxford")),
                _clean_text(row.get("tag")),
                _to_int(row.get("bnc")),
                _to_int(row.get("frq")),
                _clean_text(row.get("exchange")),
            )
        )
        if len(batch) >= 5000:
            conn.executemany(sql, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        total += len(batch)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('source', ?)", (source,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('entries', ?)", (str(total),)
    )
    conn.commit()
    return total


def iter_csv(csv_path: Path):
    csv.field_size_limit(10 * 1024 * 1024)  # 个别词条释义很长
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            yield row


def clone_ecdict(work_dir: Path) -> Path:
    """git clone --depth 1，返回 ecdict.csv 路径。失败抛 RuntimeError。"""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    print(f"[clone] {ECDICT_REPO} -> {work_dir} (~190MB, 需要磁盘空间)")
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", ECDICT_REPO, str(work_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git clone 失败（网络/代理问题？）:\n"
            + (proc.stderr or proc.stdout).strip()
            + f"\n重试方式：手工 `git clone --depth 1 {ECDICT_REPO} /tmp/ECDICT` 后跑"
            f" `python scripts/build_ecdict.py --csv /tmp/ECDICT/{CSV_NAME}`"
        )
    csv_path = work_dir / CSV_NAME
    if not csv_path.exists():
        raise RuntimeError(f"克隆成功但没找到 {CSV_NAME}，上游结构可能变了：{work_dir}")
    return csv_path


def build(out_path: Path, csv_path: Path | None, work_dir: Path, keep_clone: bool) -> int:
    cloned = False
    if csv_path is None:
        csv_path = clone_ecdict(work_dir)
        cloned = True
    elif not csv_path.exists():
        raise RuntimeError(
            f"--csv 指向的文件不存在: {csv_path}\n"
            f"重试方式：`git clone --depth 1 {ECDICT_REPO} /tmp/ECDICT` 后跑"
            f" `python scripts/build_ecdict.py --csv /tmp/ECDICT/{CSV_NAME}`"
        )
    try:
        print(f"[build] {csv_path} -> {out_path}")
        conn = create_db(out_path)
        total = write_rows(conn, iter_csv(csv_path), source=f"ECDICT ({ECDICT_REPO})")
        conn.close()
    finally:
        if cloned and not keep_clone and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            print(f"[clean] 已删除克隆目录 {work_dir}")
    return total


def build_mini(out_path: Path) -> int:
    conn = create_db(out_path)
    total = write_rows(conn, iter(mini_rows()), source="mini fixture (self-authored)")
    conn.close()
    return total


def sample(out_path: Path, n: int = 20) -> None:
    conn = sqlite3.connect(str(out_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT word, phonetic, translation, tag FROM ecdict "
        "WHERE translation IS NOT NULL AND phonetic IS NOT NULL "
        "AND frq > 0 ORDER BY frq LIMIT ?",
        (n,),
    ).fetchall()
    print(f"--- 抽样 {len(rows)} 词（人工核对用） ---")
    for r in rows:
        gloss = (r["translation"] or "").split("\\n")[0][:40]
        print(f"  {r['word']:<16} [{r['phonetic'] or ''}]  {gloss}  ({r['tag'] or '-'})")
    conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 data/ecdict.db")
    ap.add_argument("-o", "--out", default="data/ecdict.db", help="输出 sqlite 路径")
    ap.add_argument("--csv", default=None, help="已下载的 ecdict.csv（跳过 clone）")
    ap.add_argument(
        "--work-dir",
        default=str(Path(tempfile.gettempdir()) / "ecdict_src"),
        help="git clone 的临时目录",
    )
    ap.add_argument("--keep-clone", action="store_true", help="构建后保留克隆目录")
    ap.add_argument("--mini", action="store_true", help="只生成 100 词自造 mini 词典")
    ap.add_argument("--sample", type=int, default=0, help="构建后抽样打印 N 条")
    args = ap.parse_args(argv)

    out_path = Path(args.out)
    if args.mini:
        total = build_mini(out_path)
        print(f"[ok] mini 词典 {out_path}: {total} 条（自造内容，仅供测试）")
    else:
        try:
            total = build(
                out_path,
                Path(args.csv) if args.csv else None,
                Path(args.work_dir),
                args.keep_clone,
            )
        except RuntimeError as exc:
            print(f"[fail] {exc}", file=sys.stderr)
            print(
                "[hint] 网络受限时可先跑 `python scripts/build_ecdict.py --mini "
                "-o data/ecdict_mini.db` 拿到 100 词离线夹具。",
                file=sys.stderr,
            )
            return 2
        print(f"[ok] {out_path}: {total} 条词目")
    if args.sample:
        sample(out_path, args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
