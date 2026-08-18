"""ECDICT 本地词典的查询与 Lexeme 回填口径（工单 9 抽层，DESIGN §7 重构债）。

以前这些东西住在 app/server.py 里，annotate worker 为了复用同一套口径得
`from app.server import ...`——一个后台 worker 反向依赖 web 层，import 一次就把
整条 fastapi/starlette/pydantic 链条拖进来。现在独立成模块：

- 只依赖 sqlite3 + 标准库（+ app.consts），**不 import fastapi**；
- server 与 annotate 都从这里拿 EcdictStore / fill_from_ecdict，口径只有一份。

口径（行为与抽层前逐字节一致）：
- 查询：`WHERE word_lower = ? ORDER BY (word = word_lower) DESC LIMIT 1`
  （word_lower 非唯一，优先取本身就是小写的那条）。
- 命中优先级：lemma → surface（'cousins' 没收录但 'cousin' 收录，反之亦然）。
- 中文释义优先、退回英文释义；释义里的字面 "\\n" 分隔符原样保留，不折行。
- ecdict.db 不存在/损坏 → 静默降级为「查不到」（in_dict=false），绝不抛给调用方。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from app.consts import DEFAULT_ECDICT
from app.db import ConnRegistry, PathLike

__all__ = [
    "DEFAULT_ECDICT",
    "EcdictStore",
    "dominant_pos",
    "gloss_of",
    "needs_dict_fill",
    "fill_from_ecdict",
]

# ECDICT 的 pos 列形如 "n:53/v:47"
_POS_RATIO_RE = re.compile(r"^([a-z]+):(\d+)$")


class EcdictStore(ConnRegistry):
    """ecdict.db 的只读查询（缺文件/坏文件 → 静默降级为「查不到」）。"""

    def __init__(self, path: PathLike) -> None:
        super().__init__()
        self.path = Path(path)

    @property
    def available(self) -> bool:
        return self.path.exists()

    def _conn(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            self._close_local()
            return None
        c = self._cached()
        if c is None:
            try:
                c = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
                )
                c.row_factory = sqlite3.Row
            except sqlite3.Error:
                return None
            self._register(c)
            self._cache(c)
        return c

    def _close_local(self) -> None:
        c = self._cached()
        if c is not None:
            try:
                c.close()
            except sqlite3.Error:
                pass
            with self._lock:
                if c in self._conns:
                    self._conns.remove(c)
            self._cache(None)

    def lookup(self, word: str) -> dict[str, Any] | None:
        """按 word_lower 取一条；查不到或词典不可用返回 None。"""
        if not word:
            return None
        c = self._conn()
        if c is None:
            return None
        try:
            row = c.execute(
                "SELECT word, phonetic, definition, translation, pos, tag, collins "
                "FROM ecdict WHERE word_lower = ? "
                "ORDER BY (word = word_lower) DESC LIMIT 1",
                (word.lower(),),
            ).fetchone()
        except sqlite3.Error:
            # 文件被换掉/损坏：丢弃连接，下次重开
            self._close_local()
            return None
        return dict(row) if row is not None else None


def dominant_pos(raw: str | None) -> str | None:
    """ECDICT 的 pos 列 'n:53/v:47' → 'n'；mini 夹具的 'n.' → 'n'。"""
    if not raw:
        return None
    best, best_ratio = None, -1
    for part in str(raw).split("/"):
        m = _POS_RATIO_RE.match(part.strip().lower())
        if m and int(m.group(2)) > best_ratio:
            best, best_ratio = m.group(1), int(m.group(2))
    if best:
        return best
    return str(raw).strip().rstrip(".").lower() or None


def gloss_of(row: dict[str, Any]) -> str | None:
    """中文释义优先，退回英文释义。字面 '\\n' 分隔符原样保留。"""
    return row.get("translation") or row.get("definition") or None


def needs_dict_fill(row: Any) -> bool:
    return row is None or row["dict_gloss"] is None or row["ipa"] is None


def fill_from_ecdict(
    conn: sqlite3.Connection | None,
    ecdict: EcdictStore,
    lexeme: sqlite3.Row | None,
    lemma: str,
    surface: str,
) -> tuple[dict[str, Any], bool]:
    """返回 (词典字段, in_dict)。

    命中优先级：lemma → surface（'cousins' 没收录但 'cousin' 收录，反之亦然）。
    lexeme 非空且 conn 非空时把结果回填进 Lexeme 缓存（只填 NULL 字段，不覆盖已有值）。
    """
    fields: dict[str, Any] = {
        "pos": lexeme["pos"] if lexeme is not None else None,
        "ipa": lexeme["ipa"] if lexeme is not None else None,
        "dict_gloss": lexeme["dict_gloss"] if lexeme is not None else None,
    }
    cached = fields["dict_gloss"] is not None
    if not needs_dict_fill(lexeme):
        return fields, True

    row = ecdict.lookup(lemma)
    if row is None and surface != lemma:
        row = ecdict.lookup(surface)
    if row is None:
        # 词典未收录（专名？）；已有缓存释义仍算 in_dict
        return fields, cached

    fetched = {
        "pos": dominant_pos(row.get("pos")),
        "ipa": row.get("phonetic") or None,
        "dict_gloss": gloss_of(row),
    }
    for key, val in fetched.items():
        if fields[key] is None and val is not None:
            fields[key] = val

    if lexeme is not None and conn is not None:
        try:
            with conn:
                conn.execute(
                    "UPDATE Lexeme SET pos = COALESCE(pos, ?), ipa = COALESCE(ipa, ?),"
                    " dict_gloss = COALESCE(dict_gloss, ?) WHERE id = ?",
                    (fetched["pos"], fetched["ipa"], fetched["dict_gloss"], lexeme["id"]),
                )
        except sqlite3.Error:
            pass  # 缓存回填失败不影响本次查词
    return fields, True
