"""provider 插件层：注册表 + 契约 + 输出校验（DESIGN.md §5）。

一个 provider 就是两个方法：

    annotate(batch: list[dict]) -> list[dict]   # 一一对应，顺序即对应关系
    estimate_cost(batch: list[dict]) -> float   # 人民币元，只估不扣

输入包（代码组装，一词一包，speaker 可空）::

    {"lemma":"stakeout","surface":"stakeout","pos":"n","ipa":"ˈsteɪkaʊt",
     "dict_gloss":"盯梢；监视","sentence":"I just got called in to a stakeout.",
     "speaker":null,"episode":"s01e01","t":12.33}

输出包（强制 schema，验证失败即重试）::

    {"context_gloss":"（这句里）临时被叫去执行的盯梢任务",
     "hooks":[{"type":"morph","text":"stake 桩 + out 在外……",
               "label":"拆分助记，未经词源核验"}]}

DESIGN §5 的硬规矩由**代码**兜底，不指望模型自觉：
- 没有事实区（factual 已处决），morph 拆分永远带"未经核验"标签；
- 每条 hook 必须带免责标签，缺了由 enforce_labels() 补（见下）；
- hooks 宁缺寡滥，允许空数组。

注册表用惰性导入：`get_provider("fake")` 不会连带 import httpx / 真 provider，
worker 跑 fake 时进程里没有任何 HTTP 客户端。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "Provider",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderTransientError",
    "SchemaViolation",
    "ANNOTATION_SCHEMA",
    "validate_annotation",
    "enforce_labels",
    "has_disclaimer",
    "register",
    "get_provider",
    "provider_names",
]


# --- 异常 ------------------------------------------------------------------


class ProviderError(Exception):
    """provider 层通用错误。"""


class ProviderNotConfigured(ProviderError):
    """缺 API key / 缺配置。**不可重试**——重试一万次也还是没 key。"""


class ProviderTransientError(ProviderError):
    """超时、429、5xx 之类可重试的错误。"""


class SchemaViolation(ProviderError):
    """模型输出不符合 §5 契约。可重试（换一次采样也许就对了）。"""


# --- 协议 ------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """provider 协议。实现类只要有这三样，就能被 worker 使用。"""

    name: str

    def annotate(self, batch: list[dict]) -> list[dict]:
        """一批输入包 → 一批输出包，长度与顺序必须一一对应。"""
        ...

    def estimate_cost(self, batch: list[dict]) -> float:
        """这一批的预估花费，单位人民币元。只估不扣，用于 §5 预算制截断。"""
        ...


# --- 输出 schema（DESIGN §5） ----------------------------------------------

# type 开放（morph/pun/imagery/scene/multi…），但必须是小写标识符——
# 它会直接落到 Mnemonic.kind 上，得能当键用。
HOOK_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,23}$"

ANNOTATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "poi annotate output",
    "type": "object",
    "required": ["context_gloss", "hooks"],
    "additionalProperties": False,
    "properties": {
        "context_gloss": {"type": "string", "minLength": 1, "maxLength": 400},
        "hooks": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "required": ["type", "text", "label"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "pattern": HOOK_TYPE_PATTERN},
                    "text": {"type": "string", "minLength": 1, "maxLength": 300},
                    "label": {"type": "string", "minLength": 1, "maxLength": 60},
                },
            },
        },
    },
}

# 代码兜底的免责标签（DESIGN §5：模型自报确信不算证据）
MORPH_DISCLAIMER = "未经核验"  # 规范说法；"未经词源核验" 等变体也认（见 has_disclaimer）
GENERIC_DISCLAIMER = "非词源"
MORPH_LABEL = "拆分助记，未经词源核验"
GENERIC_LABEL = "记忆钩子，非词源"


def has_disclaimer(label: str) -> bool:
    """标签是否已经把免责话说到位了（"未经…核验" / "非词源" 都算）。"""
    if not label:
        return False
    return ("未经" in label and "核验" in label) or GENERIC_DISCLAIMER in label


def _manual_validate(obj: Any) -> None:
    """jsonschema 缺席时的等价手写校验（保持两条路径行为一致）。"""
    import re

    if not isinstance(obj, dict):
        raise SchemaViolation(f"顶层不是对象: {type(obj).__name__}")
    extra = set(obj) - {"context_gloss", "hooks"}
    if extra:
        raise SchemaViolation(f"顶层多出字段: {sorted(extra)}")
    for key in ("context_gloss", "hooks"):
        if key not in obj:
            raise SchemaViolation(f"缺字段: {key}")
    gloss = obj["context_gloss"]
    if not isinstance(gloss, str) or not 1 <= len(gloss) <= 400:
        raise SchemaViolation("context_gloss 必须是 1..400 字的字符串")
    hooks = obj["hooks"]
    if not isinstance(hooks, list) or isinstance(hooks, bool):
        raise SchemaViolation("hooks 必须是数组")
    if len(hooks) > 6:
        raise SchemaViolation("hooks 最多 6 条")
    for i, hook in enumerate(hooks):
        if not isinstance(hook, dict):
            raise SchemaViolation(f"hooks[{i}] 不是对象")
        extra = set(hook) - {"type", "text", "label"}
        if extra:
            raise SchemaViolation(f"hooks[{i}] 多出字段: {sorted(extra)}")
        for key in ("type", "text", "label"):
            if not isinstance(hook.get(key), str):
                raise SchemaViolation(f"hooks[{i}].{key} 必须是字符串")
        if not re.match(HOOK_TYPE_PATTERN, hook["type"]):
            raise SchemaViolation(f"hooks[{i}].type 非法: {hook['type']!r}")
        if not 1 <= len(hook["text"]) <= 300:
            raise SchemaViolation(f"hooks[{i}].text 长度越界")
        if not 1 <= len(hook["label"]) <= 60:
            raise SchemaViolation(f"hooks[{i}].label 长度越界")


def validate_annotation(obj: Any) -> dict:
    """校验单个输出包，返回原对象；不合规抛 SchemaViolation（可重试）。"""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - 依赖已锁在 requirements.txt
        _manual_validate(obj)
        return obj
    try:
        jsonschema.validate(obj, ANNOTATION_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise SchemaViolation(f"{path}: {exc.message}") from exc
    return obj


def enforce_labels(obj: dict) -> dict:
    """代码兜底的免责标签（DESIGN §5）。返回新 dict，不改原对象。

    - type == "morph" 的 hook，标签里没有"未经核验"就补成 MORPH_LABEL；
    - 其余 hook 标签里既没有"非词源"也没有"未经核验"的，补一句"（非词源）"。

    模型忘了写不是用户该承担的风险，所以这里不重试、直接改。
    """
    hooks = []
    for hook in obj.get("hooks", []):
        h = dict(hook)
        label = h.get("label", "")
        if h.get("type") == "morph":
            if not ("未经" in label and "核验" in label):
                h["label"] = MORPH_LABEL
        elif not has_disclaimer(label):
            h["label"] = f"{label}（{GENERIC_DISCLAIMER}）" if label else GENERIC_LABEL
        hooks.append(h)
    out = dict(obj)
    out["hooks"] = hooks
    return out


# --- 注册表 ----------------------------------------------------------------

# name -> 工厂（惰性 import 真模块，避免 fake 跑测试时把 httpx 拖进来）
_REGISTRY: dict[str, Callable[..., Provider]] = {}


def register(name: str, factory: Callable[..., Provider]) -> None:
    """注册（或覆盖）一个 provider 工厂。"""
    _REGISTRY[name] = factory


def provider_names() -> list[str]:
    return sorted(set(_REGISTRY) | set(_BUILTINS))


def _make_fake(**kw: Any) -> Provider:
    from app.providers.fake import FakeProvider

    return FakeProvider(**kw)


def _make_anthropic(**kw: Any) -> Provider:
    from app.providers.anthropic_api import AnthropicProvider

    return AnthropicProvider(**kw)


def _make_deepseek(**kw: Any) -> Provider:
    from app.providers.deepseek import DeepSeekProvider

    return DeepSeekProvider(**kw)


_BUILTINS: dict[str, Callable[..., Provider]] = {
    "fake": _make_fake,
    "anthropic": _make_anthropic,
    "deepseek": _make_deepseek,
}


def get_provider(name: str, **kwargs: Any) -> Provider:
    """按名取 provider 实例。

    构造**不发网络请求、不校验 key**——缺 key 要等到真发 HTTP 时才抛
    ProviderNotConfigured（这样离线也能查看 prompt、算预算、跑 dry-run）。
    """
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key) or _BUILTINS.get(key)
    if factory is None:
        raise ProviderError(
            f"未知 provider: {name!r}；可用: {', '.join(provider_names())}"
        )
    return factory(**kwargs)
