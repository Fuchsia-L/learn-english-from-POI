#!/usr/bin/env python3
"""预热入队：当集 lemma ∩ 词表 → 按词频降序 → 低优先级排进 AnnotationJob。

DESIGN §5：预热是**预算制**——这里只管入队（priority=0），花不花钱、花多少
由 `python -m app.annotate --budget` 决定。用户点击收藏的词是 priority=10，
永远插在预热词前面。

    python scripts/prefetch.py --db data/poi.db --content-id 1 \
        --wordlist data/cet46.txt --limit 200

已经 queued / running / done 的词跳过（失败过的允许重排）。
词频用 wordfreq 的离线表，不联网。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db  # noqa: E402
from app.ingest import normalize_surface  # noqa: E402

PREFETCH_PRIORITY = 0  # 低优先级（DESIGN §5）

WORDLIST_HINT = """\
[hint] 词表 {path} 不存在。它是一行一个单词的纯文本（小写即可，# 开头为注释）。
       手上已有 data/ecdict.db 的话，一条命令就能生成 CET4/6 词表：

         python - <<'EOF'
         import sqlite3, pathlib
         c = sqlite3.connect("data/ecdict.db")
         rows = c.execute("SELECT DISTINCT word_lower FROM ecdict "
                          "WHERE tag LIKE '%cet4%' OR tag LIKE '%cet6%' "
                          "ORDER BY word_lower").fetchall()
         pathlib.Path("data/cet46.txt").write_text(
             "\\n".join(r[0] for r in rows), encoding="utf-8")
         print(len(rows), "词 ->", "data/cet46.txt")
         EOF

       还没建词典就先跑 `python scripts/build_ecdict.py`（DESIGN §1 data/ 目录不入库）。\
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_wordlist(path: str | Path) -> set[str]:
    """一行一词，# 注释、空行忽略，统一小写归一。"""
    words: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        w = line.split("#", 1)[0].strip()
        if not w:
            continue
        words.add(normalize_surface(w))
    return words


def content_lemmas(conn: sqlite3.Connection, content_id: int) -> set[str]:
    """这一集出现过的全部 lemma（读 ingest 写好的 Segment.tokens_json）。"""
    lemmas: set[str] = set()
    for row in conn.execute(
        "SELECT tokens_json FROM Segment WHERE content_id = ?", (content_id,)
    ):
        raw = row["tokens_json"]
        if not raw:
            continue
        try:
            tokens = json.loads(raw)
        except ValueError:
            continue
        for t in tokens if isinstance(tokens, list) else []:
            if isinstance(t, dict) and t.get("lemma"):
                lemmas.add(str(t["lemma"]))
    return lemmas


def by_frequency(lemmas: Iterable[str], warn=None) -> list[str]:
    """词频降序（wordfreq 离线表）。没装 wordfreq 就退回字母序。"""
    words = sorted(lemmas)
    try:
        from wordfreq import zipf_frequency
    except ImportError:  # pragma: no cover - wordfreq 已锁在 requirements.txt
        if warn:
            warn("未安装 wordfreq，改用字母序（不影响入队结果，只影响截断顺序）")
        return words
    return sorted(words, key=lambda w: (-zipf_frequency(w, "en"), w))


def prefetch(
    db_path: str | Path,
    content_id: int,
    wordlist: set[str],
    limit: int | None = None,
    priority: int = PREFETCH_PRIORITY,
    dry_run: bool = False,
    log=print,
) -> dict:
    """入队，返回统计。幂等：重复跑不会产生第二条队列任务。"""
    conn = init_db(db_path)
    try:
        content = conn.execute(
            "SELECT id, title, season_ep FROM Content WHERE id = ?", (content_id,)
        ).fetchone()
        if content is None:
            raise LookupError(f"content {content_id} 不存在（先跑 app.ingest）")

        lemmas = content_lemmas(conn, content_id)
        hit = lemmas & wordlist
        ranked = by_frequency(hit, warn=lambda m: log(f"[prefetch] 警告: {m}"))

        queued, skipped_existing, missing_lexeme = [], [], []
        for lemma in ranked:
            if limit is not None and len(queued) >= limit:
                break
            lex = conn.execute(
                "SELECT id FROM Lexeme WHERE lemma = ?", (lemma,)
            ).fetchone()
            if lex is None:  # 理论上 ingest 都建过，防御一下
                missing_lexeme.append(lemma)
                continue
            lexeme_id = int(lex["id"])
            busy = conn.execute(
                "SELECT 1 FROM AnnotationJob WHERE lexeme_id = ? "
                "AND status IN ('queued','running','done') LIMIT 1",
                (lexeme_id,),
            ).fetchone()
            if busy is not None:
                skipped_existing.append(lemma)
                continue
            if not dry_run:
                conn.execute(
                    "INSERT INTO AnnotationJob (lexeme_id, status, priority, created_at)"
                    " VALUES (?, 'queued', ?, ?)",
                    (lexeme_id, priority, _now()),
                )
            queued.append(lemma)
        if not dry_run:
            conn.commit()

        return {
            "content_id": content_id,
            "title": content["title"],
            "season_ep": content["season_ep"],
            "episode_lemmas": len(lemmas),
            "wordlist": len(wordlist),
            "matched": len(hit),
            "queued": len(queued),
            "queued_lemmas": queued,
            "skipped_existing": len(skipped_existing),
            "missing_lexeme": missing_lexeme,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python scripts/prefetch.py",
        description="当集 lemma ∩ 词表 → 低优先级入队（DESIGN §5 预热）",
    )
    ap.add_argument("--db", default="data/poi.db")
    ap.add_argument("--content-id", type=int, required=True)
    ap.add_argument("--wordlist", default="data/cet46.txt")
    ap.add_argument("--limit", type=int, default=None, help="最多入队多少词")
    ap.add_argument("--priority", type=int, default=PREFETCH_PRIORITY)
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    args = ap.parse_args(argv)

    wl_path = Path(args.wordlist)
    if not wl_path.is_file():
        print(WORDLIST_HINT.format(path=wl_path), file=sys.stderr)
        return 2
    words = load_wordlist(wl_path)
    if not words:
        print(f"[fail] 词表 {wl_path} 是空的", file=sys.stderr)
        return 2

    try:
        stats = prefetch(
            db_path=args.db,
            content_id=args.content_id,
            wordlist=words,
            limit=args.limit,
            priority=args.priority,
            dry_run=args.dry_run,
        )
    except LookupError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 2

    tag = "（dry-run，未写库）" if stats["dry_run"] else ""
    print(
        f"[prefetch] {stats['title']} {stats['season_ep']} "
        f"(content_id={stats['content_id']}){tag}\n"
        f"  当集 lemma      : {stats['episode_lemmas']}\n"
        f"  词表            : {stats['wordlist']}\n"
        f"  命中            : {stats['matched']}\n"
        f"  入队(priority={args.priority}) : {stats['queued']}\n"
        f"  已有任务跳过    : {stats['skipped_existing']}"
    )
    if stats["missing_lexeme"]:
        print(f"  没有 Lexeme 行  : {len(stats['missing_lexeme'])}（先跑 app.ingest）")
    head = stats["queued_lemmas"][:15]
    if head:
        print("  词频降序前几个  : " + ", ".join(head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
