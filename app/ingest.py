"""srt → Content/Segment/token/lemma 入库（DESIGN.md §2 §3 §6）。

用法:
    python -m app.ingest <srt文件> --title "Person of Interest" \\
        --season-ep s01e01 --video /path/ep.mp4 --db data/poi.db
    # 顺带回填 extract_hardsub.py --boxes-json 产出的词级包围盒（播放器热区）
    python -m app.ingest ep.en.srt --title ... --season-ep s01e01 \\
        --boxes-json ep.boxes.json

规则（验收标准 §6 ingest 行）:
- token 统一小写归一；
- 缩写词（it's / don't / ex-con）保留为整 token，首尾标点剥离；
- 不做专有名词排除（人名地名一样进词表）；
- 同句重复词各自成 token（各带自己的 char 偏移）；
- 词元归一走 simplemma（DESIGN §7 默认选型），lemma 一律小写。

幂等：同一 (title, season_ep) 重复 ingest 只更新不新增；srt 变短时清掉多余段。
词框回填同样幂等（按 (content_id, idx) 覆盖写，不追加）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Sequence

import simplemma

from app.db import init_db

LANG = "en"

# --- srt 解析 -------------------------------------------------------------

# 00:00:12,333 --> 00:00:14,667  （毫秒分隔符逗号或点都收；尾部定位参数忽略）
_TIME_RE = re.compile(
    r"(?P<sh>\d{1,3}):(?P<sm>\d{1,2}):(?P<ss>\d{1,2})[,.](?P<sms>\d{1,3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{1,3}):(?P<em>\d{1,2}):(?P<es>\d{1,2})[,.](?P<ems>\d{1,3})"
)

# token：以字母开头结尾，内部允许撇号/连字符（it's、don't、ex-con）。
# [^\W\d_] = unicode 字母（含 café 这类重音字符），排除数字和下划线。
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’ʼ\-][^\W\d_]+)*", re.UNICODE)

# 字幕里常见的非台词标记：<i>…</i>、{\an8} 之类
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>|\{[^}]*\}")


@dataclass(frozen=True)
class Cue:
    """一条 srt cue。text 保留原始换行（多行 cue 不合并）。"""

    idx: int
    t_start: float
    t_end: float
    text: str


@dataclass(frozen=True)
class Token:
    """字幕段里的一个可点击词。char 偏移相对该段 text_en。"""

    surface: str  # 小写归一后的形式
    lemma: str  # 小写词元
    char_start: int
    char_end: int


def parse_timestamp(text: str) -> tuple[float, float]:
    """'00:00:12,333 --> 00:00:14,667' → (12.333, 14.667)，单位秒。"""
    m = _TIME_RE.search(text)
    if not m:
        raise ValueError(f"无法解析时间轴: {text!r}")
    g = m.groupdict()
    start = (
        int(g["sh"]) * 3600
        + int(g["sm"]) * 60
        + int(g["ss"])
        + int(g["sms"].ljust(3, "0")) / 1000.0
    )
    end = (
        int(g["eh"]) * 3600
        + int(g["em"]) * 60
        + int(g["es"])
        + int(g["ems"].ljust(3, "0")) / 1000.0
    )
    return start, end


def parse_srt(text: str) -> list[Cue]:
    """解析 srt 文本 → Cue 列表。

    容错：BOM、CRLF、序号缺失、块间多空行、块内多行文本、末尾无空行。
    没有时间轴的块直接丢弃（OCR 产物偶尔会有脏块）。
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n+", text.strip("\n"))
    cues: list[Cue] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        time_pos = next((i for i, ln in enumerate(lines) if _TIME_RE.search(ln)), None)
        if time_pos is None:
            continue
        try:
            t_start, t_end = parse_timestamp(lines[time_pos])
        except ValueError:
            continue
        body_lines = [_clean_line(ln) for ln in lines[time_pos + 1 :]]
        body = "\n".join(ln for ln in body_lines if ln != "")
        if body.strip() == "":
            continue
        cues.append(Cue(idx=len(cues) + 1, t_start=t_start, t_end=t_end, text=body))
    return cues


def parse_srt_file(path: str | Path) -> list[Cue]:
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_srt(raw)


def _clean_line(line: str) -> str:
    """剥格式标签，压缩空白（不动标点，char 偏移基于清洗后的文本）。"""
    line = _TAG_RE.sub("", line)
    line = line.replace(" ", " ")
    return re.sub(r"[ \t]+", " ", line).strip()


# --- 分词 / 词元归一 -------------------------------------------------------


def normalize_surface(raw: str) -> str:
    """token 统一小写归一：小写 + 撇号变体统一成 ASCII '。"""
    s = unicodedata.normalize("NFC", raw).lower()
    return s.replace("’", "'").replace("ʼ", "'")


# 缩合形的确定性前置规则（simplemma 在这类词上不可靠：it's→its、i'm→i'm）。
# 只处理英语固定的一小撮 clitic，其余一律交给 simplemma。
_CLITICS = frozenset({"s", "re", "ve", "ll", "d", "m"})  # it's / we're / they've / he'll / i'd / i'm
_NT_BASES = {"ca": "can", "wo": "will", "sha": "shall", "ai": "be"}  # can't / won't / shan't / ain't
# 情态动词各自成词元（simplemma 会把 should 归到 shall、might 归到 may）
_MODALS = frozenset(
    {"should", "would", "could", "might", "must", "can", "will", "shall", "may", "ought"}
)


def lemmatize(surface: str) -> str:
    """词元归一：缩合形走确定性规则，其余交给 simplemma。

    输入已小写；输出强制小写（simplemma 会吐大写 'I'）。
    """
    if not surface:
        return surface
    if surface in _MODALS:
        return surface
    # don't → do、shouldn't → should、won't → will
    if surface.endswith("n't") and len(surface) > 3:
        base = surface[:-3]
        return _NT_BASES.get(base) or lemmatize(base)
    # it's → it、i'm → i、they've → they、cousin's → cousin
    if "'" in surface:
        stem, _, clitic = surface.rpartition("'")
        if stem and clitic in _CLITICS:
            return lemmatize(stem)
    try:
        lemma = simplemma.lemmatize(surface, lang=LANG)
    except Exception:  # simplemma 对怪字符可能抛，退回原形
        lemma = surface
    lemma = normalize_surface(lemma or surface)
    return lemma or surface


def tokenize(text: str) -> list[Token]:
    """把一段字幕切成 token 列表。

    - 只认含字母的串，纯数字/纯标点跳过；
    - 首尾标点自然被正则排除，内部撇号/连字符保留；
    - 同句重复词各自成 token（不去重）。
    """
    tokens: list[Token] = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        surface = normalize_surface(raw)
        if not surface:
            continue
        tokens.append(
            Token(
                surface=surface,
                lemma=lemmatize(surface),
                char_start=m.start(),
                char_end=m.end(),
            )
        )
    return tokens


def tokens_to_json(tokens: Sequence[Token]) -> str:
    return json.dumps([asdict(t) for t in tokens], ensure_ascii=False)


# --- 入库 ------------------------------------------------------------------


def upsert_content(
    conn: sqlite3.Connection,
    title: str,
    season_ep: str,
    video_path: str | None,
    srt_path: str | None,
) -> int:
    conn.execute(
        "INSERT INTO Content (title, season_ep, video_path, srt_path) VALUES (?,?,?,?) "
        "ON CONFLICT (title, season_ep) DO UPDATE SET "
        "  video_path = COALESCE(excluded.video_path, Content.video_path),"
        "  srt_path   = COALESCE(excluded.srt_path,   Content.srt_path)",
        (title, season_ep, video_path, srt_path),
    )
    row = conn.execute(
        "SELECT id FROM Content WHERE title = ? AND season_ep = ?", (title, season_ep)
    ).fetchone()
    return int(row["id"])


def get_or_create_lexeme(conn: sqlite3.Connection, lemma: str) -> int:
    """按 lemma 取 Lexeme（不存在则建骨架行）。

    ipa / dict_gloss / pos 留空，由 ECDICT 在 /lookup 时回填——
    Lexeme 是客观词典缓存，ingest 只负责让词元有个稳定主键。
    """
    row = conn.execute("SELECT id FROM Lexeme WHERE lemma = ?", (lemma,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO Lexeme (lemma) VALUES (?)", (lemma,))
    return int(cur.lastrowid)


def upsert_wordform(conn: sqlite3.Connection, surface: str, lexeme_id: int) -> None:
    conn.execute(
        "INSERT INTO WordForm (surface, lexeme_id) VALUES (?, ?) "
        "ON CONFLICT (surface) DO UPDATE SET lexeme_id = excluded.lexeme_id",
        (surface, lexeme_id),
    )


def ingest_cues(
    conn: sqlite3.Connection,
    content_id: int,
    cues: Iterable[Cue],
) -> dict:
    """把 cue 写成 Segment + WordForm/Lexeme，返回统计。幂等。"""
    kept_idx: list[int] = []
    n_tokens = 0
    surface_to_lemma: dict[str, str] = {}

    for cue in cues:
        tokens = tokenize(cue.text)
        conn.execute(
            "INSERT INTO Segment (content_id, idx, t_start, t_end, text_en, tokens_json) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (content_id, idx) DO UPDATE SET "
            "  t_start = excluded.t_start, t_end = excluded.t_end,"
            "  text_en = excluded.text_en, tokens_json = excluded.tokens_json",
            (
                content_id,
                cue.idx,
                cue.t_start,
                cue.t_end,
                cue.text,
                tokens_to_json(tokens),
            ),
        )
        kept_idx.append(cue.idx)
        n_tokens += len(tokens)
        for tok in tokens:
            surface_to_lemma[tok.surface] = tok.lemma

    # srt 变短时清掉上一次留下的多余段
    if kept_idx:
        placeholders = ",".join("?" * len(kept_idx))
        conn.execute(
            f"DELETE FROM Segment WHERE content_id = ? AND idx NOT IN ({placeholders})",
            [content_id, *kept_idx],
        )
    else:
        conn.execute("DELETE FROM Segment WHERE content_id = ?", (content_id,))

    # 词表：先建 lexeme 再挂 surface（不排专名）
    lemmas = sorted(set(surface_to_lemma.values()))
    lemma_ids = {lemma: get_or_create_lexeme(conn, lemma) for lemma in lemmas}
    for surface in sorted(surface_to_lemma):
        upsert_wordform(conn, surface, lemma_ids[surface_to_lemma[surface]])

    return {
        "segments": len(kept_idx),
        "tokens": n_tokens,
        "unique_surfaces": len(surface_to_lemma),
        "unique_lemmas": len(lemmas),
    }


# --- 词级包围盒回填（DESIGN §4 热区） --------------------------------------
# 输入是 scripts/extract_hardsub.py --boxes-json 的产物：
#   [{idx, start, end, text, words: [{w, x, y, width, height}]}]
# idx 与 srt 序号（1 起）一一对应，即 Segment.idx。坐标是视频原始帧像素，
# x=null 表示该词丢框（OCR 置信度过低）——丢框不丢词，前端跳过该词热区。
# 这里只按 idx 认段、把 words 数组**原样**存进 Segment.word_boxes_json，
# 不做坐标变换（前端按 video 实际显示尺寸缩放，见 static/player.html）。


def _warn(msg: str) -> None:
    print(f"[ingest] 警告: {msg}", file=sys.stderr)


def load_boxes(path: str | Path) -> list:
    """读 --boxes-json 文件。顶层必须是列表。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"boxes-json 顶层应为列表，实际是 {type(data).__name__}")
    return data


def _same_text(a: str | None, b: str | None) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", (s or "")).strip().lower()  # noqa: E731
    return norm(a) == norm(b)


def apply_boxes(
    conn: sqlite3.Connection,
    content_id: int,
    entries: Iterable,
    warn=_warn,
) -> dict:
    """把词框按 idx 回填到对应 Segment.word_boxes_json。

    幂等：同一份 boxes 重复跑结果一致（覆盖写，不追加）。
    对不上的 idx / 结构不合法的条目只告警不中断（OCR 产物与 srt 可能不同批次）。
    """
    known = {
        int(r["idx"]): r["text_en"]
        for r in conn.execute(
            "SELECT idx, text_en FROM Segment WHERE content_id = ?", (content_id,)
        )
    }
    applied = 0
    skipped = 0
    missing: list[int] = []
    text_mismatch: list[int] = []
    seen: set[int] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            skipped += 1
            warn(f"boxes 条目不是对象，跳过: {entry!r:.60}")
            continue
        raw_idx = entry.get("idx")
        try:
            idx = int(raw_idx)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            skipped += 1
            warn(f"boxes 条目缺少可用的 idx，跳过: {raw_idx!r}")
            continue
        words = entry.get("words")
        if words is None:
            words = []
        if not isinstance(words, list):
            skipped += 1
            warn(f"idx={idx} 的 words 不是列表，跳过")
            continue
        if idx not in known:
            missing.append(idx)
            warn(f"idx={idx} 在 content_id={content_id} 里没有对应字幕段，跳过")
            continue
        if idx in seen:
            warn(f"idx={idx} 在 boxes 里重复出现，后者覆盖前者")
        if not _same_text(entry.get("text"), known[idx]):
            text_mismatch.append(idx)
            warn(f"idx={idx} 的 boxes.text 与库里 text_en 不一致（仍按 idx 回填）")
        seen.add(idx)
        conn.execute(
            "UPDATE Segment SET word_boxes_json = ? WHERE content_id = ? AND idx = ?",
            (json.dumps(words, ensure_ascii=False), content_id, idx),
        )
        applied += 1

    uncovered = sorted(set(known) - seen)
    if missing:
        warn(f"共 {len(missing)} 个 idx 对不上字幕段: {missing[:10]}")
    if uncovered:
        warn(f"共 {len(uncovered)} 个字幕段没有词框（前端走自渲染退路）: {uncovered[:10]}")
    return {
        "boxes_applied": applied,
        "boxes_skipped": skipped,
        "boxes_missing_idx": missing,
        "boxes_text_mismatch_idx": text_mismatch,
        "segments_without_boxes": uncovered,
    }


def ingest_srt(
    db_path: str | Path,
    srt_path: str | Path,
    title: str,
    season_ep: str,
    video_path: str | None = None,
    conn: sqlite3.Connection | None = None,
    boxes_path: str | Path | None = None,
) -> dict:
    """入口：解析 srt 并写库，返回统计 dict。

    给了 boxes_path 就在同一事务里回填词框（见 apply_boxes）。
    """
    cues = parse_srt_file(srt_path)
    entries = load_boxes(boxes_path) if boxes_path else None
    own_conn = conn is None
    conn = conn or init_db(db_path)
    try:
        with conn:
            content_id = upsert_content(
                conn, title, season_ep, video_path, str(srt_path)
            )
            stats = ingest_cues(conn, content_id, cues)
            if entries is not None:
                stats.update(apply_boxes(conn, content_id, entries))
        stats["content_id"] = content_id
        stats["title"] = title
        stats["season_ep"] = season_ep
        return stats
    finally:
        if own_conn:
            conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.ingest",
        description="srt → Content/Segment/token/lemma 入库",
    )
    ap.add_argument("srt", help="srt 文件路径")
    ap.add_argument("--title", required=True, help="剧名，例：Person of Interest")
    ap.add_argument("--season-ep", required=True, help="集号，例：s01e01")
    ap.add_argument("--video", default=None, help="视频文件路径（/media 接口用）")
    ap.add_argument("--db", default="data/poi.db", help="SQLite 路径")
    ap.add_argument(
        "--boxes-json",
        default=None,
        help="extract_hardsub.py --boxes-json 的产物，按 idx 回填词级包围盒（播放器热区）",
    )
    args = ap.parse_args(argv)

    stats = ingest_srt(
        db_path=args.db,
        srt_path=args.srt,
        title=args.title,
        season_ep=args.season_ep,
        video_path=args.video,
        boxes_path=args.boxes_json,
    )
    print(
        f"[ingest] {args.title} {args.season_ep} -> content_id={stats['content_id']}\n"
        f"  segments        : {stats['segments']}\n"
        f"  tokens          : {stats['tokens']}\n"
        f"  unique surfaces : {stats['unique_surfaces']}\n"
        f"  unique lemmas   : {stats['unique_lemmas']}"
    )
    if args.boxes_json:
        print(
            f"  word boxes      : {stats['boxes_applied']} 段回填"
            f"（跳过 {stats['boxes_skipped']}，对不上 idx {len(stats['boxes_missing_idx'])}，"
            f"无框段 {len(stats['segments_without_boxes'])}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
