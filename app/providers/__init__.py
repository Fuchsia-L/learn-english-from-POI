"""provider 插件层：注册表 + 契约 + 输出校验（DESIGN.md §5）。

一个 provider 就是两个方法：

    annotate(batch: list[dict]) -> list[dict]   # 按 id 对应，**顺序无语义**
    estimate_cost(batch: list[dict]) -> float   # 人民币元，只估不扣

输入包（代码组装，一词一包，speaker 可空）::

    {"id":"17","lemma":"stakeout","surface":"stakeout","pos":"n","ipa":"ˈsteɪkaʊt",
     "dict_gloss":"盯梢；监视","sentence":"I just got called in to a stakeout.",
     "speaker":null,"episode":"s01e01","t":12.33}

输出包（强制 schema，验证失败即重试）::

    {"id":"17","context_gloss":"（这句里）临时被叫去执行的盯梢任务",
     "hooks":[{"type":"morph","text":"stake 桩 + out 在外……",
               "label":"拆分助记，未经词源核验"}]}

**批量对位靠 id，不靠顺序**（工单 6-2）：模型重排数组曾导致助记张冠李戴。
每条输入包带一个稳定 id（worker 用 AnnotationJob.id），模型必须原样回传；
match_annotations() 按 id 匹配，缺 id / id 不在本批 / 重复 id 的元素一律丢弃，
对应的任务留给下一轮重试。数组顺序从此没有任何语义。

DESIGN §5 的硬规矩由**代码**兜底，不指望模型自觉：
- 没有事实区（factual 已处决），morph 拆分永远带"未经核验"标签；
- 每条 hook 必须带免责标签，缺了由 enforce_labels() 补（见下）；
- hooks 宁缺寡滥，允许空数组。

注册表用惰性导入：`get_provider("fake")` 不会连带 import httpx / 真 provider，
worker 跑 fake 时进程里没有任何 HTTP 客户端。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence, runtime_checkable

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
    "coerce_id",
    "pack_id",
    "match_annotations",
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
        """一批输入包 → 一批输出包。每个输出包必须带输入包的 id（顺序无所谓）；
        对不上 id 的元素会被丢弃，对应任务由 worker 重试。"""
        ...

    def estimate_cost(self, batch: list[dict]) -> float:
        """这一批的预估花费，单位人民币元。只估不扣，用于 §5 预算制截断。"""
        ...


# --- 输出 schema（DESIGN §5） ----------------------------------------------

# type 开放（morph/pun/imagery/scene/multi…），但必须是小写标识符——
# 它会直接落到 Mnemonic.kind 上，得能当键用。
HOOK_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,23}$"

# id 由代码生成（AnnotationJob.id 的十进制字符串），模型只负责原样抄回来
ID_MAX_LEN = 64

ANNOTATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "poi annotate output",
    "type": "object",
    "required": ["id", "context_gloss", "hooks"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": ID_MAX_LEN},
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
    extra = set(obj) - {"id", "context_gloss", "hooks"}
    if extra:
        raise SchemaViolation(f"顶层多出字段: {sorted(extra)}")
    for key in ("id", "context_gloss", "hooks"):
        if key not in obj:
            raise SchemaViolation(f"缺字段: {key}")
    rid = obj["id"]
    if not isinstance(rid, str) or not 1 <= len(rid) <= ID_MAX_LEN:
        raise SchemaViolation(f"id 必须是 1..{ID_MAX_LEN} 字的字符串")
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


# --- 按 id 对位（工单 6-2：模型重排不许串词） ------------------------------


def coerce_id(value: Any) -> str | None:
    """把模型回传的 id 归一成字符串；归一不了返回 None（该元素作废）。

    容忍模型把 "17" 写成数字 17（JSON 里两种都常见），但不容忍 1.5 / null /
    对象这类根本对不上号的东西。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else None
    if isinstance(value, str):
        s = value.strip()
        return s if 1 <= len(s) <= ID_MAX_LEN else None
    return None


def pack_id(item: dict) -> str:
    """输入包的 id。代码组装时一定会写 id；缺了就退回 lemma/surface（够稳定）。"""
    rid = coerce_id(item.get("id"))
    if rid is not None:
        return rid
    return str(item.get("lemma") or item.get("surface") or "")


def match_annotations(
    batch: Sequence[dict], items: Any
) -> tuple[dict[str, dict], list[str]]:
    """把模型输出按 id 对回输入包。返回 (id -> 校验并补标签后的输出, 问题列表)。

    顺序在这里**没有任何语义**。丢弃规则（丢掉的那条 = 对应任务下轮重试）：
    - 元素不是对象 / 缺 id / id 归一不了；
    - id 不在本批（模型自己编的）；
    - id 重复：前后两条至少有一条是错的，**两条都丢**，绝不赌；
    - 过不了 ANNOTATION_SCHEMA。
    """
    wanted = {pack_id(i) for i in batch}
    matched: dict[str, dict] = {}
    poisoned: set[str] = set()
    problems: list[str] = []
    if not isinstance(items, list):
        return {}, [f"输出顶层不是数组: {type(items).__name__}"]

    for pos, el in enumerate(items):
        if not isinstance(el, dict):
            problems.append(f"[{pos}] 不是对象: {type(el).__name__}")
            continue
        rid = coerce_id(el.get("id"))
        if rid is None:
            problems.append(f"[{pos}] 缺 id 或 id 非法: {el.get('id')!r}")
            continue
        if rid not in wanted:
            problems.append(f"[{pos}] id={rid!r} 不在本批")
            continue
        if rid in poisoned:
            problems.append(f"[{pos}] id={rid!r} 重复（已作废）")
            continue
        if rid in matched:
            del matched[rid]
            poisoned.add(rid)
            problems.append(f"[{pos}] id={rid!r} 重复：两条都丢弃")
            continue
        el = dict(el)
        el["id"] = rid
        try:
            matched[rid] = enforce_labels(validate_annotation(el))
        except SchemaViolation as exc:
            problems.append(f"[{pos}] id={rid!r} schema: {exc}")
    return matched, problems


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
