"""真 provider 的公共骨架：组装 → 发请求 → 解析 → 校验 → 重试。

anthropic_api.py / deepseek.py 只需要填四件事：端点、鉴权头、请求体形状、
怎么从响应里掏出文本。其余全在这儿，两家共用一套解析与重试策略。

**没有 API key 时**：构造对象、拼 prompt、估价都能正常跑（离线可调提示词），
唯独真要发 HTTP 的 `_post()` 抛 ProviderNotConfigured。测试因此可以完整覆盖
组装逻辑而一分钱不花、一个包不发。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Sequence

from app.providers import (
    ProviderError,
    ProviderNotConfigured,
    ProviderTransientError,
    SchemaViolation,
    enforce_labels,
    validate_annotation,
)
from app.providers.prompts import SYSTEM_PROMPT, build_user_prompt, debug_dump

# ```json ... ``` 包裹（模型总爱加，虽然 prompt 里说了别加）
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# 兜底：从一堆废话里抠出最外层 JSON 数组
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

# 可重试的 HTTP 状态码（其余一律当硬错误，重试只会白烧钱）
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

CJK_LO, CJK_HI, FW_LO, FW_HI = 0x3000, 0x9fff, 0xff00, 0xffef
_CJK_RE = re.compile(chr(CJK_LO) + "-" + chr(CJK_HI) + chr(FW_LO) + "-" + chr(FW_HI))
_CJK_RE = re.compile("[" + chr(CJK_LO) + "-" + chr(CJK_HI) + chr(FW_LO) + "-" + chr(FW_HI) + "]")


def approx_tokens(text: str) -> int:
    """粗估 token 数：中日韩字符 1 个 ≈ 1 token，其余 4 字符 ≈ 1 token。

    只用来做预算截断（DESIGN §5），宁可高估——高估的后果是少花钱。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4
