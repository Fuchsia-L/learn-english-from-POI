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

**牌价（工单 16）**：写在 `PRICE`（`app.providers.base.Price` 结构：单价 +
currency + source + as_of + note），`as_of` 会跟着估价结果和 `--dry-run` 打印
一起出现——牌价是**某一天抄的一个数**，别当常识用。不改代码也能覆盖：
`POI_DEEPSEEK_PRICE_IN` / `POI_DEEPSEEK_PRICE_OUT`（元 / 百万 token）。

**模型（工单 8a）**：默认 `deepseek-v4-flash`。老的 `deepseek-chat` /
`deepseek-reasoner` 已被官方宣布退役，别再往那两个名字上打。v4 系列**默认开
thinking 且 effort=high**，不显式关掉会按推理档重度计费，所以请求体里永远带
`"thinking": {"type": "disabled"}`——助记生成不需要长思维链，多花的钱纯属白烧。
"""

from __future__ import annotations

from typing import Sequence

from app.providers.base import ChatJSONProvider, Price
from app.providers.prompts import build_messages

DEFAULT_MODEL = "deepseek-v4-flash"

# --- 牌价（**变价改这里**）--------------------------------------------------
# deepseek-v4-flash 的官网人民币价目，单位都是 **元 / 百万 token**。官网直接标
# 人民币，所以这里不再走"美元 × 汇率"那一道（少一个会漂的估计量）。
#
#   峰时（保守上界，**估价只认这两个数**）：
#       输入 cache-miss  ¥3.00     输出  ¥9.00
#   下面这些只作记录，**不参与估算**：
#       错峰：峰价减半（输入 cache-miss ¥1.50 / 输出 ¥4.50）
#       缓存命中：¥0.10（错峰再减半 ¥0.05）
#
# 峰时 = **北京时间 09:00–12:00 与 14:00–18:00**，其余时段按错峰计。
PRICE_SOURCE = "official price page (api-docs.deepseek.com/quick_start/pricing)"
PRICE_AS_OF = "2026-08-16"  # 抄下这组数的日期。牌价是**某一天的**，不是常识。
PRICE_NOTE = "估算用，以官方现价为准（峰时 + cache-miss，最贵那档）"

PRICE_IN_MISS_PEAK_CNY_PER_MTOK = 3.0     # 输入 cache-miss，峰时 —— 估价用
PRICE_OUT_PEAK_CNY_PER_MTOK = 9.0         # 输出，峰时 —— 估价用
# 以下四个常量仅供查阅/对账，估价一律不用（见下面的口径说明）
PRICE_IN_MISS_OFFPEAK_CNY_PER_MTOK = 1.5  # 输入 cache-miss，错峰（峰价减半）
PRICE_IN_HIT_PEAK_CNY_PER_MTOK = 0.10     # 输入 cache-hit，峰时
PRICE_IN_HIT_OFFPEAK_CNY_PER_MTOK = 0.05  # 输入 cache-hit，错峰
PRICE_OUT_OFFPEAK_CNY_PER_MTOK = 4.5      # 输出，错峰（峰价减半）

# 峰时窗口（北京时间），只用于文档/日志说明，不参与计算。
PEAK_HOURS_BEIJING = ((9, 12), (14, 18))

# 估价口径（硬规矩）：一律按 **峰时 + cache-miss** 算，即牌价里最贵的那一档。
# 预算是硬顶不是期望值——错峰/命中缓存省下来的钱算意外之喜，不许提前花掉。
PRICE_IN_CNY_PER_MTOK = PRICE_IN_MISS_PEAK_CNY_PER_MTOK
PRICE_OUT_CNY_PER_MTOK = PRICE_OUT_PEAK_CNY_PER_MTOK

# 估价真正认的那一份（工单 16-2：单价 + 币种 + 来源 + as_of + 备注 打成一个结构）。
PRICE = Price(
    input_per_mtok=PRICE_IN_CNY_PER_MTOK,
    output_per_mtok=PRICE_OUT_CNY_PER_MTOK,
    currency="CNY",
    source=PRICE_SOURCE,
    as_of=PRICE_AS_OF,
    note=PRICE_NOTE,
)

# 其它档位只作对账，估价一律不用（保守原则：只按最贵那档估）。
REFERENCE_PRICES_CNY_PER_MTOK = {
    "in_miss_peak": PRICE_IN_MISS_PEAK_CNY_PER_MTOK,
    "in_miss_offpeak": PRICE_IN_MISS_OFFPEAK_CNY_PER_MTOK,
    "in_hit_peak": PRICE_IN_HIT_PEAK_CNY_PER_MTOK,
    "in_hit_offpeak": PRICE_IN_HIT_OFFPEAK_CNY_PER_MTOK,
    "out_peak": PRICE_OUT_PEAK_CNY_PER_MTOK,
    "out_offpeak": PRICE_OUT_OFFPEAK_CNY_PER_MTOK,
}

# 环境变量覆盖（工单 16-2）：牌价变了又懒得改代码时，
#     POI_DEEPSEEK_PRICE_IN=2.4 POI_DEEPSEEK_PRICE_OUT=7.2 python -m app.annotate ...
# 单位同上（元 / 百万 token）。值非法（负数 / NaN / 非数 / 空串）不会被忽略，
# 而是让估价直接失败 —— worker 按"估价不可用"停手，一分钱不花（工单 8b）。
PRICE_ENV_PREFIX = "POI_DEEPSEEK_PRICE"

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

    # 官方人民币牌价（峰时 cache-miss）+ 它的来源/日期；可被环境变量覆盖。
    price = PRICE
    price_env_prefix = PRICE_ENV_PREFIX
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
