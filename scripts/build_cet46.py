#!/usr/bin/env python3
"""构建预热词表 data/cet46.txt（DESIGN §1 data/ 目录不入库，§5 预热预算制）。

数据源：GitHub 公开仓库 mahavivo/english-wordlists 的 `CET4_edited.txt` +
`CET6_edited.txt`（大学英语四/六级大纲词表）。走 `git clone --depth 1`
（raw.githubusercontent.com 在本机可能被代理拦，不依赖它），用完删除克隆目录。

**克隆目录的安全边界（工单 16-1）**：`--work-dir` 给的是"在哪儿放克隆目录"，
脚本在它内部用 `tempfile.mkdtemp` 开一个唯一子目录，事后只 rmtree 这个子目录；
用户给的目录本身和里面原有的东西一概不动（老版本会 `rmtree(work_dir)`，
`--work-dir ~/Documents` 就是一场事故）。不给 `--work-dir` 时在系统临时目录里开。

用法::

    python scripts/build_cet46.py                       # 克隆 → 合并 → 清理
    python scripts/build_cet46.py --source /tmp/wl/CET4_edited.txt \\
                                  --source /tmp/wl/CET6_edited.txt   # 用本地文件
    python scripts/build_cet46.py -o data/cet46.txt --dry-run        # 只统计不写

处理口径（与 app/ingest 完全一致，保证能和 Segment.tokens_json 的 lemma 对上）:
- 每行取行首那个英文词：`abandon [əˈbændən] vt.丢弃` → `abandon`；
  `instruct[ inˈstrʌkt]`（原始数据里少个空格）、`toward(s)`、`systematic(al)`
  一样取到词干；`oˈclock` 里的修饰符撇号先归一成 ASCII `'`。
- 小写归一走 `ingest.normalize_surface`，词元归一走 `ingest.lemmatize`（simplemma）。
- 丢弃：中文标题行、`(共 4615 词)` 这类计数行、字母分节头（单个 B/C/D…；
  `a`、`i` 是真词，保留）。
- 合并去重后按字母序输出，一行一词（开头两行 `#` 注释记来源，prefetch 会忽略）。

网络不通不静默失败：打印失败原因 + 手工重试方式，退出码 2。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.consts import DEFAULT_WORDLIST  # noqa: E402
from app.ingest import lemmatize, normalize_surface  # noqa: E402

WORDLIST_REPO = "https://github.com/mahavivo/english-wordlists.git"
# 克隆用的一次性子目录前缀（脚本只删自己用这个前缀 mkdtemp 出来的目录）
CLONE_PREFIX = "cet46_src_"
# 仓库里的四级 / 六级大纲词表（顺序即统计里的「增量」口径：先四级，六级只算新增）
SOURCE_NAMES = ("CET4_edited.txt", "CET6_edited.txt")

# 行首词：字母开头，内部允许撇号/连字符。`[`、`(`、空格、中文都是天然终止符。
_HEAD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# 词表里混着各种「像撇号的字符」（o'clock 被写成 oˈclock）
_APOSTROPHES = {"’": "'", "ʼ": "'", "ˈ": "'", "‘": "'", "´": "'", "`": "'"}
# 单字母里只有这俩是真词，其余（分节头 B/C/D…）一律丢
SINGLE_LETTER_WORDS = frozenset({"a", "i"})


def extract_word(line: str) -> str | None:
    """一行 → 行首的那个英文词（已小写归一）；不是词条行返回 None。"""
    s = (line or "").strip().lstrip("﻿")
    if not s or s.startswith("#"):
        return None
    for bad, good in _APOSTROPHES.items():
        s = s.replace(bad, good)
    m = _HEAD_RE.match(s)
    if m is None:
        return None
    word = normalize_surface(m.group(0)).strip("'-")
    if not word:
        return None
    if len(word) == 1 and word not in SINGLE_LETTER_WORDS:
        return None
    return word


def parse_wordlist(lines: Iterable[str]) -> list[str]:
    """词表文本 → 词列表（保持出现顺序，同一文件内去重，未做 lemma 归一）。"""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        w = extract_word(line)
        if w is None or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def read_source(path: str | Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_wordlist(text.splitlines())


def merge_sources(sources: Sequence[tuple[str, list[str]]]) -> dict:
    """按给定顺序合并去重 + lemma 归一，返回 {words, per_source}。

    per_source 里的 `added` 是**增量**：CET6 只算它比 CET4 多出来的那些
    （DESIGN 预期 CET4 约 4k + CET6 约 2k 增量）。
    """
    merged: set[str] = set()
    per_source = []
    for name, words in sources:
        lemmas = [lemmatize(w) or w for w in words]
        uniq = sorted(set(lemmas))
        added = sorted(set(uniq) - merged)
        merged |= set(uniq)
        per_source.append(
            {
                "name": name,
                "words": len(words),
                "lemmas": len(uniq),
                "changed_by_lemma": sum(1 for w, l in zip(words, lemmas) if w != l),
                "added": len(added),
            }
        )
    return {"words": sorted(merged), "per_source": per_source}


def write_wordlist(path: str | Path, words: Sequence[str], source: str) -> int:
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = f"# CET4+CET6 预热词表（lemma 归一，小写）\n# 来源: {source}  生成: {stamp}\n"
    p.write_text(header + "\n".join(words) + "\n", encoding="utf-8")
    return len(words)


# --- 取词表 ----------------------------------------------------------------


def make_clone_dir(work_dir: str | Path | None) -> Path:
    """给这次克隆开一个**本脚本自己创建的**空目录，返回它。

    安全边界（工单 16-1）：用户用 --work-dir 给的目录**永远不被删除、不被清空**，
    脚本只在它**内部**用 `tempfile.mkdtemp` 开一个唯一子目录，克隆和事后清理都
    只碰这个子目录。不给 --work-dir 时就在系统临时目录里 mkdtemp（行为不变）。
    以前的写法是 `rmtree(work_dir)` —— 用户随手传 `--work-dir ~/Documents`
    就会把整个目录递归删掉。
    """
    if work_dir is None:
        return Path(tempfile.mkdtemp(prefix=CLONE_PREFIX))
    base = Path(work_dir).expanduser()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=CLONE_PREFIX, dir=base))
    except OSError as exc:
        raise RuntimeError(
            f"--work-dir 不可用: {base}（{exc}）；给一个可写目录，或省略该参数"
            f"（默认用系统临时目录）"
        ) from exc


def clone_wordlists(work_dir: Path) -> list[Path]:
    """git clone --depth 1 到 work_dir（必须是本脚本开的空目录），返回词表路径。

    本函数**不删除任何东西**：目录的生命周期由 make_clone_dir/build 管。
    失败抛 RuntimeError。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)  # mkdtemp 已建好，这里只是兜底
    print(f"[clone] {WORDLIST_REPO} -> {work_dir}（约 11MB，用完删除）")
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", WORDLIST_REPO, str(work_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git clone 失败（网络/代理问题？）:\n"
            + (proc.stderr or proc.stdout).strip()
            + f"\n重试方式：手工 `git clone --depth 1 {WORDLIST_REPO} /tmp/wordlists`"
            + " 之后跑 `python scripts/build_cet46.py"
            + "".join(f" --source /tmp/wordlists/{n}" for n in SOURCE_NAMES)
            + "`"
        )
    paths = [work_dir / n for n in SOURCE_NAMES]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        raise RuntimeError(
            f"克隆成功但缺文件 {missing}，上游结构可能变了：{work_dir}\n"
            f"用 --source 指定实际文件名即可绕过。"
        )
    return paths


def _cleanup_clone(clone_root: Path | None, keep_clone: bool) -> None:
    """只删脚本自己 mkdtemp 出来的那个子目录，绝不碰用户给的 --work-dir 本身。"""
    if clone_root is None:
        return
    if keep_clone:
        print(f"[keep] 保留克隆目录 {clone_root}（--keep-clone）")
        return
    if not clone_root.name.startswith(CLONE_PREFIX):  # 防呆：不是我开的就不删
        print(f"[clean] 跳过删除（不是本脚本创建的目录）: {clone_root}")
        return
    if clone_root.exists():
        shutil.rmtree(clone_root, ignore_errors=True)
        print(f"[clean] 已删除克隆目录 {clone_root}")


def build(
    out_path: Path,
    sources: Sequence[Path] | None,
    work_dir: Path | None = None,
    keep_clone: bool = False,
    dry_run: bool = False,
) -> dict:
    """取词表 → 合并去重 + lemma 归一 → 写文件，返回统计。

    work_dir 是**放克隆目录的地方**，不是克隆目录本身：真正被 rmtree 的永远是
    脚本自己在它里面 mkdtemp 出来的一次性子目录（工单 16-1）。None = 系统临时目录。
    """
    clone_root: Path | None = None
    if sources:
        missing = [str(p) for p in sources if not Path(p).is_file()]
        if missing:
            raise RuntimeError(f"--source 指向的文件不存在: {missing}")
        paths = [Path(p) for p in sources]
        origin = "本地文件: " + ", ".join(p.name for p in paths)
    else:
        clone_root = make_clone_dir(work_dir)
        origin = WORDLIST_REPO
        try:
            paths = clone_wordlists(clone_root)
        except BaseException:  # 克隆失败也要收走自己开的那个临时子目录
            _cleanup_clone(clone_root, keep_clone)
            raise
    try:
        loaded = [(p.name, read_source(p)) for p in paths]
        merged = merge_sources(loaded)
    finally:
        _cleanup_clone(clone_root, keep_clone)

    written = 0
    if not dry_run:
        written = write_wordlist(out_path, merged["words"], origin)
    return {
        "out": str(out_path),
        "source": origin,
        "per_source": merged["per_source"],
        "total": len(merged["words"]),
        "written": written,
        "dry_run": dry_run,
    }


def report(stats: dict) -> None:
    tag = "（dry-run，未写文件）" if stats["dry_run"] else ""
    print(f"[build_cet46] 来源: {stats['source']}{tag}")
    for s in stats["per_source"]:
        print(
            f"  {s['name']:<20} 抽出 {s['words']:>5} 词 → {s['lemmas']:>5} lemma"
            f"（其中 {s['changed_by_lemma']} 个被词元归一改写），本表新增 {s['added']:>5}"
        )
    print(f"  合计唯一 lemma      : {stats['total']}")
    if not stats["dry_run"]:
        print(f"  已写入              : {stats['out']}（一行一词，# 开头为注释）")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python scripts/build_cet46.py",
        description="构建 CET4+CET6 预热词表（DESIGN §5 预热）",
    )
    ap.add_argument("-o", "--out", default=DEFAULT_WORDLIST, help="输出词表路径")
    ap.add_argument(
        "-s",
        "--source",
        action="append",
        default=None,
        help="用本地词表文件替代联网克隆（可给多次，按给的顺序统计增量）",
    )
    ap.add_argument(
        "--work-dir",
        default=None,
        help="放克隆目录的父目录（默认系统临时目录）。脚本在它**内部**新建一个唯一"
        "子目录来克隆，用完只删这个子目录——你给的目录本身及其原有内容一律不动",
    )
    ap.add_argument("--keep-clone", action="store_true", help="构建后保留克隆目录")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args(argv)

    try:
        stats = build(
            out_path=Path(args.out),
            sources=[Path(s) for s in args.source] if args.source else None,
            work_dir=Path(args.work_dir) if args.work_dir else None,
            keep_clone=args.keep_clone,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        print(
            "[hint] 联网构建失败不影响其它模块：prefetch 没有词表时会打印生成方法并"
            "以非零码退出，播放/查词/收藏一切照常。",
            file=sys.stderr,
        )
        return 2
    report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
