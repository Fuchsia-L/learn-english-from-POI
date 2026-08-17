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


class ChatJSONProvider:
    """聊天补全型 provider 的共同实现。子类填空即可。"""

    name = "chat"
    env_var = ""  # 例：ANTHROPIC_API_KEY
    endpoint = ""
    model = ""
    # 牌价：人民币元 / 百万 token。子类覆盖；随时可能过期，自己核对官网。
    price_in_cny_per_mtok = 0.0
    price_out_cny_per_mtok = 0.0
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

    def estimate_cost(self, batch: list[dict]) -> float:
        """预估这一批的花费（人民币元）。不发请求，离线可算。"""
        if not batch:
            return 0.0
        prompt = SYSTEM_PROMPT + build_user_prompt(batch)
        tin = approx_tokens(prompt)
        tout = self.out_tokens_per_item * len(batch)
        cost = (
            tin * self.price_in_cny_per_mtok + tout * self.price_out_cny_per_mtok
        ) / 1_000_000
        return round(cost, 6)

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
