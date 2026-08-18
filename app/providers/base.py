"""真 provider 的公共骨架：组装 → 发请求 → 解析 → 校验 → 重试。

anthropic_api.py / deepseek.py 只需要填四件事：端点、鉴权头、请求体形状、
怎么从响应里掏出文本。其余全在这儿，两家共用一套解析与重试策略。

**没有 API key 时**：构造对象、拼 prompt、估价都能正常跑（离线可调提示词），
唯独真要发 HTTP 的 `_post()` 抛 ProviderNotConfigured。测试因此可以完整覆盖
组装逻辑而一分钱不花、一个包不发。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from app.providers import (
    ProviderError,
    ProviderNotConfigured,
    ProviderTransientError,
    SchemaViolation,
    match_annotations,
    pack_id,
)
from app.providers.prompts import SYSTEM_PROMPT, build_user_prompt, debug_dump

# ```json ... ``` 包裹（模型总爱加，虽然 prompt 里说了别加）
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# 兜底：从一堆废话里抠出最外层 JSON 数组
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

# 可重试的 HTTP 状态码（其余一律当硬错误，重试只会白烧钱）
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

_CJK_RE = re.compile("[　-鿿＀-￯]")


def approx_tokens(text: str) -> int:
    """粗估 token 数：中日韩字符 1 个 ≈ 1 token，其余 4 字符 ≈ 1 token。

    只用来做预算截断（DESIGN §5），宁可高估——高估的后果是少花钱。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def parse_json_array(text: str) -> list:
    """模型输出文本 → JSON 数组。扒代码块、扒废话，最后必须是 list。"""
    if not isinstance(text, str) or not text.strip():
        raise SchemaViolation("模型返回空文本")
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1).strip()
    try:
        data = json.loads(body)
    except ValueError:
        m = _ARRAY_RE.search(body)
        if not m:
            raise SchemaViolation(f"响应里找不到 JSON 数组: {body[:120]!r}") from None
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise SchemaViolation(f"JSON 解析失败: {exc}") from exc
    if isinstance(data, dict):
        # 强制 JSON-object 模式（DeepSeek response_format）会包一层 {"items": [...]}；
        # 单条时模型也可能直接吐一个结果对象。两种都容忍。
        lists = [v for v in data.values() if isinstance(v, list)]
        if "items" in data and isinstance(data["items"], list):
            data = data["items"]
        elif len(data) == 1 and len(lists) == 1:
            data = lists[0]
        else:
            data = [data]
    if not isinstance(data, list):
        raise SchemaViolation(f"顶层应为数组，实际是 {type(data).__name__}")
    return data


# --- 牌价（工单 16-2：一个结构 + 元数据 + 可覆盖 + 非法即 fail closed） -----


class PriceConfigError(ProviderError):
    """牌价配置非法（覆盖值是负数 / NaN / 非数，或子类根本没填牌价）。

    **不可重试**，而且故意让它从 estimate_cost 里抛出去：worker 的
    `_estimate` 把估价异常一律当"估价不可用"处理（工单 8b 的 fail closed
    路径），于是配错价 = 一分钱都不花地停手，而不是按一个瞎猜的数照跑。
    """


@dataclass(frozen=True)
class Price:
    """一份牌价 + 它的来龙去脉。

    单价单位固定是 **currency / 百万 token**。带 `as_of` 是因为牌价是**某一天
    抄下来的一个数**，不是常识：不把日期摆在明面上，半年后没人知道该不该信它。
    """

    input_per_mtok: float
    output_per_mtok: float
    currency: str = "CNY"
    source: str = "official price page"
    as_of: str = "2026-08-16"
    note: str = "估算用，以官方现价为准"

    @property
    def symbol(self) -> str:
        return {"CNY": "¥", "USD": "$"}.get(self.currency, f"{self.currency} ")

    def label(self) -> str:
        """一行人话，dry-run / 日志里直接打印。"""
        s = self.symbol
        return (
            f"输入 {s}{self.input_per_mtok:g} / 输出 {s}{self.output_per_mtok:g} "
            f"每百万 token（{self.currency}；来源: {self.source}；"
            f"as_of={self.as_of}；{self.note}）"
        )

    def stamp(self) -> str:
        """极短版：只给 as_of，塞进已经很长的日志行。"""
        return f"牌价 as_of={self.as_of}"

    def as_dict(self) -> dict:
        return {
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "currency": self.currency,
            "source": self.source,
            "as_of": self.as_of,
            "note": self.note,
        }

    def validated(self, where: str = "牌价") -> "Price":
        """单价必须是有限非负实数，否则抛 PriceConfigError。"""
        for field, value in (
            ("input_per_mtok", self.input_per_mtok),
            ("output_per_mtok", self.output_per_mtok),
        ):
            _check_price_number(value, f"{where}.{field}")
        return self


def _check_price_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or value is None:
        raise PriceConfigError(f"{where} 非法: {value!r}（不是数）")
    try:
        val = float(value)
    except (TypeError, ValueError) as exc:
        raise PriceConfigError(f"{where} 非法: {value!r}（转不成数字: {exc}）") from exc
    if math.isnan(val) or math.isinf(val):
        raise PriceConfigError(f"{where} 非法: {val}（NaN/inf）")
    if val < 0:
        raise PriceConfigError(f"{where} 非法: {val}（负数）")
    return val


# 子类没填牌价时的占位：估价时会被 validated() 之外的显式检查拦下来
UNPRICED = Price(0.0, 0.0, source="未填", as_of="未填", note="子类必须覆盖 price")


class CostEstimate(float):
    """估价结果：值就是钱（float，老调用方完全无感），另挂着这次用的牌价。

    工单 16-2 要求"estimate_cost 的输出带 as_of 标注"。返回类型仍是 float 的
    子类，所以 `float(est)`、`est > budget`、`f"{est:.4f}"`、`math.isnan(est)`
    一律照旧，只是多了 `.price` / `.as_of` 能问。
    """

    price: Price

    def __new__(cls, value: float, price: Price) -> "CostEstimate":
        obj = super().__new__(cls, value)
        obj.price = price
        return obj

    @property
    def as_of(self) -> str:
        return self.price.as_of

    def label(self) -> str:
        return f"{self.price.symbol}{float(self):.6f}（{self.price.label()}）"

    def __repr__(self) -> str:  # 日志里打出来自带日期
        return f"CostEstimate({float(self)!r}, {self.price.stamp()})"


class ChatJSONProvider:
    """聊天补全型 provider 的共同实现。子类填空即可。"""

    name = "chat"
    env_var = ""  # 例：ANTHROPIC_API_KEY
    endpoint = ""
    model = ""
    # 牌价：一个 Price 结构（单价 + currency + source + as_of + note）。子类必填。
    # 随时可能过期 —— as_of 就是给这件事留的把手。
    price: Price = UNPRICED
    # 环境变量覆盖前缀：设了就认 f"{prefix}_IN" / f"{prefix}_OUT"
    # （例：POI_DEEPSEEK_PRICE_IN=2.4 POI_DEEPSEEK_PRICE_OUT=7.2）
    price_env_prefix = ""
    # 每个词条预留的输出 token（一条 gloss + 至多 3 条 hook 的经验值）
    out_tokens_per_item = 220

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 1.0,
        max_tokens: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[dict], dict] | None = None,
    ) -> None:
        """
        api_key 不给就读环境变量（读不到也不报错，等发请求时才抛）。
        transport 是给测试/自定义网络栈的注入点：dict 请求体 → dict 响应体。
        """
        self._api_key = api_key
        self.model = model or self.model
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._max_tokens = max_tokens
        self._sleep = sleep
        self._transport = transport
        # 上一次 annotate() 里被丢弃的元素（缺 id / id 不在本批 / 重复 / 不合 schema）
        self.last_problems: list[str] = []

    # --- 配置 --------------------------------------------------------------

    @property
    def api_key(self) -> str | None:
        return self._api_key or os.environ.get(self.env_var) or None

    # --- 牌价 --------------------------------------------------------------

    def _legacy_price(self) -> Price | None:
        """兼容老式写法：子类直接把两个 float 挂在类上（而不是给 Price）。

        本类把 price_in/out_cny_per_mtok 变成了只读属性，子类若仍用类属性覆盖，
        属性查找会先命中子类的那个数——这时按它们造一份 Price，别让老代码摔。
        """
        vals = []
        for attr in ("price_in_cny_per_mtok", "price_out_cny_per_mtok"):
            raw = getattr(type(self), attr, None)
            if isinstance(raw, property):  # 没被子类覆盖，走新结构
                return None
            vals.append(raw)
        return Price(
            input_per_mtok=_check_price_number(vals[0], f"{self.name}.price_in_cny_per_mtok"),
            output_per_mtok=_check_price_number(vals[1], f"{self.name}.price_out_cny_per_mtok"),
            source="子类的 price_in/out_cny_per_mtok 类属性（老式写法）",
            as_of="未标注",
            note="估算用，以官方现价为准",
        )

    def base_price(self) -> Price:
        """子类填的那份牌价（未经环境变量覆盖）。没填 / 形状不对就抛。"""
        legacy = self._legacy_price()
        if legacy is not None:
            return legacy
        price = self.price
        if not isinstance(price, Price):
            raise PriceConfigError(
                f"{self.name}: price 应该是 Price 结构，实际是 {type(price).__name__}"
            )
        if price is UNPRICED:
            raise PriceConfigError(
                f"{self.name}: 没有牌价（price 没被子类覆盖），估价不可用"
            )
        return price

    def resolved_price(self) -> Price:
        """真正用于估价的牌价 = 子类牌价 + 环境变量覆盖，并且**校验过**。

        覆盖口径（工单 16-2）：`{price_env_prefix}_IN` / `_OUT`，单位与牌价一致
        （currency / 百万 token），两个可以只设一个。值非法（负数 / NaN / 非数 /
        空串）一律抛 PriceConfigError —— 估价失败按 fail closed 处理，不许拿一个
        看不懂的数去花钱。
        """
        price = self.base_price().validated(f"{self.name} 牌价")
        prefix = self.price_env_prefix
        if not prefix:
            return price
        names = (f"{prefix}_IN", f"{prefix}_OUT")
        raw_in, raw_out = (os.environ.get(n) for n in names)
        if raw_in is None and raw_out is None:
            return price
        used = [n for n, v in zip(names, (raw_in, raw_out)) if v is not None]
        return replace(
            price,
            input_per_mtok=(
                price.input_per_mtok if raw_in is None
                else _check_price_number(raw_in.strip(), names[0])
            ),
            output_per_mtok=(
                price.output_per_mtok if raw_out is None
                else _check_price_number(raw_out.strip(), names[1])
            ),
            source=f"环境变量 {'+'.join(used)} 覆盖（原: {price.source}）",
            as_of=f"env-override（表内原值 as_of={price.as_of}）",
            note=f"覆盖值来自本机环境变量；{price.note}",
        )

    def price_label(self) -> str:
        """一行牌价说明（含 as_of），给 dry-run / 启动日志打印。"""
        return self.resolved_price().label()

    @property
    def price_in_cny_per_mtok(self) -> float:
        """老名字，等于 resolved_price().input_per_mtok（含环境变量覆盖）。"""
        return self.resolved_price().input_per_mtok

    @property
    def price_out_cny_per_mtok(self) -> float:
        """老名字，等于 resolved_price().output_per_mtok（含环境变量覆盖）。"""
        return self.resolved_price().output_per_mtok

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def require_key(self) -> str:
        key = self.api_key
        if not key:
            raise ProviderNotConfigured(
                f"{self.name}: 缺 API key。设环境变量 {self.env_var}=... "
                f"或用 --provider fake 离线跑。"
            )
        return key

    # --- 子类填空 ----------------------------------------------------------

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def payload(self, batch: Sequence[dict]) -> dict:
        raise NotImplementedError

    def extract_text(self, resp: dict) -> str:
        raise NotImplementedError

    # --- 协议 --------------------------------------------------------------

    def annotate(self, batch: list[dict]) -> list[dict]:
        """返回**按 id 对上号的那些**输出，顺序按输入排（顺序本身无语义）。

        对不上的元素直接丢：条数少于输入是合法结果，剩下的任务由 worker 下一轮
        重试（provider 自己不为部分失败重试，免得和 worker 的重试叠加烧钱）。
        一条都对不上才抛 SchemaViolation（可重试）。
        """
        if not batch:
            return []
        payload = self.payload(batch)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._post(payload)
                items = parse_json_array(self.extract_text(resp))
                matched, problems = match_annotations(batch, items)
                if not matched:
                    raise SchemaViolation(
                        f"{self.name}: 没有一条输出能按 id 对回输入"
                        + ("；" + "；".join(problems[:3]) if problems else "")
                    )
                self.last_problems = problems
                return [matched[k] for i in batch if (k := pack_id(i)) in matched]
            except (ProviderTransientError, SchemaViolation) as exc:
                last = exc
                if attempt < self.retries:
                    self._sleep(self.backoff * (2**attempt))
        assert last is not None
        raise last

    def estimate_cost(self, batch: list[dict]) -> CostEstimate:
        """预估这一批的花费。不发请求，离线可算。

        返回的是 float 子类 CostEstimate：数值照常参与预算比较，另外挂着这次用的
        Price（`.as_of` / `.price.label()`），好让"这是某天的牌价"一直跟着结果走。
        牌价配置非法时抛 PriceConfigError —— worker 会按估价不可用停手（工单 8b）。
        """
        price = self.resolved_price()
        if not batch:
            return CostEstimate(0.0, price)
        prompt = SYSTEM_PROMPT + build_user_prompt(batch)
        tin = approx_tokens(prompt)
        tout = self.out_tokens_per_item * len(batch)
        cost = (
            tin * price.input_per_mtok + tout * price.output_per_mtok
        ) / 1_000_000
        return CostEstimate(round(cost, 6), price)

    # --- HTTP --------------------------------------------------------------

    def max_tokens(self, batch: Sequence[dict]) -> int:
        if self._max_tokens is not None:
            return self._max_tokens
        return max(512, self.out_tokens_per_item * len(batch) + 256)

    def _post(self, payload: dict) -> dict:
        """唯一真正发包的地方。缺 key 直接抛 ProviderNotConfigured。"""
        self.require_key()  # 缺 key 在这里就断，绝不摸网络
        if self._transport is not None:  # 注入的假传输层（测试用）
            return self._transport(payload)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ProviderNotConfigured(f"{self.name}: 缺 httpx，pip install httpx") from exc

        headers = dict(self.headers())
        headers.setdefault("content-type", "application/json")
        try:
            resp = httpx.post(
                self.endpoint, json=payload, headers=headers, timeout=self.timeout
            )
        except Exception as exc:  # 连接失败/超时 → 可重试
            raise ProviderTransientError(f"{self.name}: 请求失败 {exc}") from exc
        if resp.status_code in RETRYABLE_STATUS:
            raise ProviderTransientError(
                f"{self.name}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        if resp.status_code in (401, 403):
            raise ProviderNotConfigured(
                f"{self.name}: HTTP {resp.status_code}，key 无效或无权限"
            )
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SchemaViolation(f"{self.name}: 响应不是 JSON") from exc
        if not isinstance(data, dict):
            raise SchemaViolation(f"{self.name}: 响应顶层不是对象")
        return data

    # --- 调试 --------------------------------------------------------------

    def dump_prompt(self, batch: Sequence[dict]) -> str:
        """离线打印完整 prompt（调提示词用，不发请求）。"""
        return debug_dump(batch)

    def _key_value(self, resp: dict, *path: Any) -> Any:
        cur: Any = resp
        for p in path:
            if isinstance(p, int):
                if not isinstance(cur, list) or len(cur) <= p:
                    raise SchemaViolation(f"{self.name}: 响应结构异常，缺 {path}")
                cur = cur[p]
            else:
                if not isinstance(cur, dict) or p not in cur:
                    raise SchemaViolation(f"{self.name}: 响应结构异常，缺 {path}")
                cur = cur[p]
        return cur
