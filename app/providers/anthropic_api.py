"""Anthropic Messages API provider（骨架已完整，缺 key 就抛）。

DESIGN §5：钩子生成的目标提供方是 DeepSeek，Anthropic 先用低档位糙跑。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db \
    --provider anthropic --once --budget 4.0
```

没设 ANTHROPIC_API_KEY 时，annotate() 会抛 ProviderNotConfigured
（构造、拼 prompt、estimate_cost 仍然可用，方便离线调提示词）。
"""

from __future__ import annotations

from typing import Sequence

from app.providers.base import ChatJSONProvider, Price
from app.providers.prompts import SYSTEM_PROMPT, build_user_prompt

DEFAULT_MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"

# --- 牌价（结构与 deepseek.py 那份**同形**，工单 16-2；数值未改）------------
# **来源和 DeepSeek 那份不一样，别混**：Anthropic 官网只标美元，这两个数是
# claude-haiku 档 $1/M 输入、$5/M 输出按汇率 ≈7.2 折出来的**量级粗估**，
# 既不是官方人民币牌价，也没核过现价 —— 所以 as_of 写的是 "unverified" 而不是
# 某个日期，别让它冒充"某天抄的官方价"。主力 provider 是 DeepSeek，
# Anthropic 只做糙跑备份；真要拿它花钱，先核对官网价目表再改这里。
PRICE_IN_CNY_PER_MTOK = 7.2
PRICE_OUT_CNY_PER_MTOK = 36.0
USD_PRICE_PER_MTOK = {"input": 1.0, "output": 5.0}  # 折算依据，仅供对账
USD_CNY_RATE = 7.2

PRICE = Price(
    input_per_mtok=PRICE_IN_CNY_PER_MTOK,
    output_per_mtok=PRICE_OUT_CNY_PER_MTOK,
    currency="CNY",
    source=(
        "美元牌价 $1/$5 每百万 token × 汇率≈7.2 折算（**非**官方人民币牌价）"
    ),
    as_of="unverified（建库时的量级粗估，未核对官网现价）",
    note="估算用，以官方现价为准；Anthropic 只做糙跑备份",
)

# 环境变量覆盖（同 DeepSeek 口径）：POI_ANTHROPIC_PRICE_IN / _OUT，元 / 百万 token。
PRICE_ENV_PREFIX = "POI_ANTHROPIC_PRICE"


class AnthropicProvider(ChatJSONProvider):
    name = "anthropic"
    env_var = "ANTHROPIC_API_KEY"
    endpoint = "https://api.anthropic.com/v1/messages"
    model = DEFAULT_MODEL

    # 牌价结构见模块顶部 PRICE（含来源/as_of 的免责说明）；可被环境变量覆盖。
    price = PRICE
    price_env_prefix = PRICE_ENV_PREFIX

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.require_key(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def payload(self, batch: Sequence[dict]) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens(batch),
            "temperature": 1.0,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": build_user_prompt(batch)},
                # 预填一个 "[" 逼模型直接进 JSON 数组，省掉"好的，以下是…"的开场白
                {"role": "assistant", "content": "["},
            ],
        }

    def extract_text(self, resp: dict) -> str:
        """content 数组里所有 text 块拼起来；预填的 '[' 要补回去。"""
        blocks = self._key_value(resp, "content")
        parts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type", "text") == "text"
        ]
        text = "".join(parts).strip()
        if text.startswith("[") or text.startswith("```"):
            return text
        return "[" + text  # assistant 预填被 API 吞掉了，补回开头的 [
