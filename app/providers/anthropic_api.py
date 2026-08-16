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

from app.providers.base import ChatJSONProvider
from app.providers.prompts import SYSTEM_PROMPT, build_user_prompt

DEFAULT_MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"


class AnthropicProvider(ChatJSONProvider):
    name = "anthropic"
    env_var = "ANTHROPIC_API_KEY"
    endpoint = "https://api.anthropic.com/v1/messages"
    model = DEFAULT_MODEL

    # 牌价换算成人民币（按 $1/M in、$5/M out × 汇率 7.2 的量级粗估）。
    # **随时可能过期**——真要看钱包请自己核对官网价目表后改这两个数。
    price_in_cny_per_mtok = 7.2
    price_out_cny_per_mtok = 36.0

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
