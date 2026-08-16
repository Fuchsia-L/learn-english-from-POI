"""DeepSeek provider（骨架已完整，缺 key 就抛）。

DESIGN §5 里点名的钩子生成目标提供方——中文谐音/联想它更顺手，价格也便宜一个量级。
接口是 OpenAI 兼容的 /chat/completions。

```bash
export DEEPSEEK_API_KEY=sk-...
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db \
    --provider deepseek --loop --budget 4.0
```

没设 DEEPSEEK_API_KEY 时 annotate() 抛 ProviderNotConfigured。
"""

from __future__ import annotations

from typing import Sequence

from app.providers.base import ChatJSONProvider
from app.providers.prompts import build_messages

DEFAULT_MODEL = "deepseek-chat"


class DeepSeekProvider(ChatJSONProvider):
    name = "deepseek"
    env_var = "DEEPSEEK_API_KEY"
    endpoint = "https://api.deepseek.com/chat/completions"
    model = DEFAULT_MODEL

    # 牌价：人民币元 / 百万 token（官网本来就用人民币计价，无需换汇）。
    # **随时可能过期**（还有夜间折扣、缓存命中价），自己核对后改这两个数。
    price_in_cny_per_mtok = 2.0
    price_out_cny_per_mtok = 8.0

    def headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.require_key()}",
            "content-type": "application/json",
        }

    def payload(self, batch: Sequence[dict]) -> dict:
        return {
            "model": self.model,
            "messages": build_messages(batch, wrap_object=True),
            "max_tokens": self.max_tokens(batch),
            "temperature": 1.3,  # 官方建议：创意写作档位
            "stream": False,
            # 强制 JSON 输出。注意 DeepSeek 的 json_object 只保证是 JSON 对象，
            # 所以 prompt 里要求的数组会被包一层——parse_json_array 两种都吃。
            "response_format": {"type": "json_object"},
        }

    def extract_text(self, resp: dict) -> str:
        return str(self._key_value(resp, "choices", 0, "message", "content") or "")
