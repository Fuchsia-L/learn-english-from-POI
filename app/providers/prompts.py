"""提示词模板（单独一个文件，方便用户自己调）。

这里只管**文本**，不管 HTTP、不管重试、不管解析——那些在 base.py。
改提示词只动本文件，改完跑 `pytest tests/test_providers.py` 看契约还成立不。

设计要点（DESIGN §5）：
- **音标进 prompt**：ipa 字段显式喂给模型，谐音钩子必须依据读音而非拼写
  （"stakeout /ˈsteɪkaʊt/ → 死盯凯特" 是读音钩子；照着字母瞎编不算）。
- **宁缺毋滥**：hooks 允许空数组，硬凑的联想比没有更伤记忆。
- **没有事实区**：morph 拆分永远标"未经词源核验"，其余钩子标"非词源"。
- 输出必须是纯 JSON 数组，每个元素**原样带回输入的 id**——解析侧按 id 对位，
  顺序没有语义（工单 6-2：曾因模型重排导致助记张冠李戴）。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from app.providers import pack_id

# --- system ----------------------------------------------------------------

SYSTEM_PROMPT = """\
你是英语词汇助记的写手，服务对象是一个用美剧学英语的中文母语成年人。
你的唯一产出是 JSON。你只负责语义与联想，装配、过滤、渲染由程序负责。

【铁律】
1. 只输出 JSON 数组，不要 Markdown 代码块、不要解释、不要前后缀文字。
2. 数组长度必须与输入条目数完全一致，每个输入条目**必须且只能**有一个输出元素。
3. 每个元素只有三个键：id、context_gloss、hooks。多一个键都算错。
   **id 必须原样抄回该条目给出的 id 字符串**（一个字符都不许改、不许编、不许复用）。
   程序按 id 对位，不看顺序；id 错了或缺了，那一条会被直接丢弃并重跑。
4. 你**没有**陈述事实的授权：不许讲词源、不许讲"来自拉丁语/古英语…"。
   任何拆分、联想都只是记忆手段，必须带免责标签。

【context_gloss】
- 用中文写，20~40 字，只解释这个词**在这句台词里**的意思和用法色彩，
  不要复述词典释义，不要举别的例句。
- 如果这句台词里该词就是最普通的用法，就老实写普通用法，别硬拗。

【hooks】
- 记忆钩子数组，0~3 条。**宁缺毋滥**：不自然、要硬凑的一律不给，空数组是合法答案。
- 每条 hook：{"type": ..., "text": ..., "label": ...}
  - type 用小写英文标识符，类型开放，常见的有：
    morph（形态拆分）、pun（谐音）、imagery（画面联想）、
    scene（结合台词场景）、multi（一词多义串联）。
  - text 用中文写，一句话说清，不超过 60 字。
  - label 是免责标签：
    - type=morph 必须写「拆分助记，未经词源核验」；
    - 其余必须包含「非词源」三个字，例如「记忆钩子，非词源」。
- **谐音钩子（pun）必须依据给出的 ipa 音标**，不是照着字母拼。
  没有 ipa、或者读音实在谐不出自然的中文，就不要给 pun。
- 不要出现低俗、暴力、歧视内容；不要用生僻典故。

【输出形状示例】（仅示形状，不要照抄内容；id 用该条目自己的 id）
[{"id":"17","context_gloss":"（这句里）临时被叫去执行的盯梢任务",
  "hooks":[{"type":"morph","text":"stake 桩 + out 在外——像钉在外面的桩一样守着",
            "label":"拆分助记，未经词源核验"},
           {"type":"pun","text":"读音近「死盯凯特」——盯梢就是死盯目标",
            "label":"记忆钩子，非词源"}]}]\
"""

# --- user ------------------------------------------------------------------

USER_HEADER = """\
下面是 {n} 个待处理词条。请严格按上面的铁律输出**长度为 {n} 的 JSON 数组**。

"""

USER_FOOTER = """
再说一遍：只输出 JSON 数组，长度 {n}，每个元素带上它那条的 id 原文（{ids}）。\
"""

# 单条词条的渲染模板。ipa 单独一行且显式标注用途，是为了让谐音钩子有依据。
# id 放第一行且再次点名"原样回传"——对位全靠它。
ITEM_TEMPLATE = """\
### 条目 {i}
- id: {id}          ← 输出该条目时必须原样回传这个 id
- 词元 lemma: {lemma}
- 台词中的形式 surface: {surface}
- 词性 pos: {pos}
- 音标 ipa: {ipa}          ← 谐音钩子请依据这个读音，不要照字母编
- 词典释义 dict_gloss: {dict_gloss}
- 台词原句 sentence: {sentence}
- 说话人 speaker: {speaker}
- 出处 episode/时间: {episode} @ {t}
"""

_EMPTY = "（无）"


def _fmt(value: Any) -> str:
    if value is None:
        return _EMPTY
    if isinstance(value, str):
        s = value.strip()
        return s if s else _EMPTY
    return str(value)


def render_item(item: dict, index: int) -> str:
    """一个输入包 → prompt 里的一段。字段缺失一律降级成"（无）"，不抛异常。"""
    t = item.get("t")
    return ITEM_TEMPLATE.format(
        i=index,
        id=pack_id(item),
        lemma=_fmt(item.get("lemma")),
        surface=_fmt(item.get("surface") or item.get("lemma")),
        pos=_fmt(item.get("pos")),
        ipa=_fmt(item.get("ipa")),
        dict_gloss=_fmt(item.get("dict_gloss")),
        sentence=_fmt(item.get("sentence")),
        speaker=_fmt(item.get("speaker")),
        episode=_fmt(item.get("episode")),
        t=f"{float(t):.2f}s" if isinstance(t, (int, float)) else _EMPTY,
    )


def build_user_prompt(batch: Sequence[dict]) -> str:
    """整批输入包 → 一条 user 消息。"""
    n = len(batch)
    body = "\n".join(render_item(item, i + 1) for i, item in enumerate(batch))
    ids = ", ".join(f'"{pack_id(item)}"' for item in batch) or "无"
    return USER_HEADER.format(n=n) + body + USER_FOOTER.format(n=n, ids=ids)


# 有的接口（DeepSeek response_format=json_object）强制顶层必须是对象，
# 与"只输出数组"冲突 —— 这时追加这句，解析侧会把 items 拆回数组。
JSON_OBJECT_WRAP_NOTE = """

【本次接口的额外约束】顶层必须是 JSON 对象，请输出
{{"items": [ ……上面要求的那个长度为 {n} 的数组…… ]}}
除 items 外不要有别的键。\
"""


def build_messages(batch: Sequence[dict], wrap_object: bool = False) -> list[dict]:
    """OpenAI 兼容格式的消息列表（DeepSeek 用；Anthropic 单独拆 system）。"""
    user = build_user_prompt(batch)
    if wrap_object:
        user += JSON_OBJECT_WRAP_NOTE.format(n=len(batch))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def debug_dump(batch: Sequence[dict]) -> str:
    """调提示词时肉眼看的完整 prompt（不发请求）。"""
    return (
        "=== SYSTEM ===\n"
        + SYSTEM_PROMPT
        + "\n\n=== USER ===\n"
        + build_user_prompt(batch)
        + "\n\n=== INPUT JSON ===\n"
        + json.dumps(list(batch), ensure_ascii=False, indent=2)
    )
