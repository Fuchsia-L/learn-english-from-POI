"""DeepSeek provider（骨架已完整，缺 key 就抛）。

DESIGN §5 里点名的钩子生成目标提供方——中文谐音/联想它更顺手，价格也便宜一个量级。
接口是 OpenAI 兼容的 /chat/completions。

```bash
export DEEPSEEK_API_KEY=sk-...
python -m app.annotate --db data/poi.db --ecdict data/ecdict.db \
    --provider deepseek --loop --budget 4.0
# 换模型（只对真 provider 有意义）
python -m app.annotate --db data/poi.db --provider deepseek --model deepseek-v4 --dry-run
```

没设 DEEPSEEK_API_KEY 时 annotate() 抛 ProviderNotConfigured。

**模型（工单 8a）**：默认 `deepseek-v4-flash`。老的 `deepseek-chat` /
`deepseek-reasoner` 已被官方宣布退役，别再往那两个名字上打。v4 系列**默认开
thinking 且 effort=high**，不显式关掉会按推理档重度计费，所以请求体里永远带
`"thinking": {"type": "disabled"}`——助记生成不需要长思维链，多花的钱纯属白烧。
"""

from __future__ import annotations

from typing import Sequence

from app.providers.base import ChatJSONProvider
from app.providers.prompts import build_messages

DEFAULT_MODEL = "deepseek-v4-flash"

# --- 牌价 ------------------------------------------------------------------
# **官方美元价 2026-08 摘录**（官网只标美元，分峰谷两档；下面单位都是
# 美元 / 百万 token）。峰时 = 01:00–04:00 与 06:00–10:00 UTC，谷时半价。
# 随时可能过期，接真 key 之前自己去官网核对一遍。
USD_PER_MTOK_IN_MISS_PEAK = 0.44    # 输入 cache-miss，峰时
USD_PER_MTOK_IN_MISS_OFF = 0.22     # 输入 cache-miss，谷时
USD_PER_MTOK_IN_HIT_PEAK = 0.014    # 输入 cache-hit，峰时
USD_PER_MTOK_IN_HIT_OFF = 0.007     # 输入 cache-hit，谷时
USD_PER_MTOK_OUT_PEAK = 1.32        # 输出，峰时
USD_PER_MTOK_OUT_OFF = 0.66         # 输出，谷时

# 汇率：近似值，**自己查当日中间价再改**。估价只用来卡预算，差几个点不影响结论，
# 但别指望它能对账。
USD_CNY = 7.2

# 估价口径（硬规矩）：一律按 **峰时 + cache-miss** 算，即牌价里最贵的那一档。
# 预算是硬顶不是期望值——谷时/命中缓存省下来的钱算意外之喜，不许提前花掉。
PRICE_IN_CNY_PER_MTOK = USD_PER_MTOK_IN_MISS_PEAK * USD_CNY   # 3.168
PRICE_OUT_CNY_PER_MTOK = USD_PER_MTOK_OUT_PEAK * USD_CNY      # 9.504

# 每个词条预留的输出 token（保守上界）。依据：
#   - context_gloss 模板要求 20~40 个汉字，按上界 40 ≈ 40 token；
#   - hooks 最多 3 条，每条 text ≤60 汉字 ≈ 60 token，label 固定话术 ≈ 12 token，
#     加 type + JSON 键名/引号/逗号的结构开销 ≈ 10 token，单条约 82 token；
#   - 每个元素的 id 与三个键名的外壳 ≈ 20 token。
# 合计 40 + 82×3 + 20 = 306，取整到 320 留余量。历史 fake/真样本的实际输出都在
# 150~260 token 之间（hooks 常常只给 1~2 条），所以 320 是稳稳的上界。
OUT_TOKENS_PER_ITEM = 320


class DeepSeekProvider(ChatJSONProvider):
    name = "deepseek"
    env_var = "DEEPSEEK_API_KEY"
    endpoint = "https://api.deepseek.com/chat/completions"
    model = DEFAULT_MODEL

    # 牌价换算成人民币元 / 百万 token（基类的估价公式用这两个）。
    price_in_cny_per_mtok = PRICE_IN_CNY_PER_MTOK
    price_out_cny_per_mtok = PRICE_OUT_CNY_PER_MTOK
    out_tokens_per_item = OUT_TOKENS_PER_ITEM

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
            # v4 系列默认 thinking 开着且 effort=high。助记生成用不上思维链，
            # 开着只会把每次调用的输出 token 翻好几倍——显式关死（工单 8a）。
            "thinking": {"type": "disabled"},
            # 强制 JSON 输出。注意 DeepSeek 的 json_object 只保证是 JSON 对象，
            # 所以 prompt 里要求的数组会被包一层——parse_json_array 两种都吃。
            "response_format": {"type": "json_object"},
        }

    def extract_text(self, resp: dict) -> str:
        return str(self._key_value(resp, "choices", 0, "message", "content") or "")
