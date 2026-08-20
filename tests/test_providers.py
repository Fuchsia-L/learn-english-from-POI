"""provider 插件层：注册表、契约、schema 校验、提示词、真 provider 骨架。

**全程离线**：本文件用 autouse 的 socket 地雷把 socket.socket 打掉，
任何一次真实网络调用都会当场炸掉测试（DESIGN §5：不接真 API，不花一分钱）。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers import (  # noqa: E402
    GENERIC_DISCLAIMER,
    has_disclaimer,
    MORPH_LABEL,
    ProviderError,
    ProviderNotConfigured,
    ProviderTransientError,
    SchemaViolation,
    coerce_id,
    enforce_labels,
    get_provider,
    match_annotations,
    pack_id,
    provider_names,
    validate_annotation,
)
from app.providers import _manual_validate  # noqa: E402
from app.providers.base import (  # noqa: E402
    ChatJSONProvider,
    Price,
    PriceConfigError,
    approx_tokens,
    parse_json_array,
)
from app.providers.deepseek import DEFAULT_MODEL as DS_DEFAULT_MODEL  # noqa: E402
from app.providers.fake import FakeInjectedFailure, FakeProvider  # noqa: E402
from app.providers.prompts import build_user_prompt, debug_dump  # noqa: E402

ITEM = {
    "id": "101",
    "lemma": "stakeout",
    "surface": "stakeout",
    "pos": "n",
    "ipa": "ˈsteɪkaʊt",
    "dict_gloss": "盯梢；监视",
    "sentence": "I just got called in to a stakeout.",
    "speaker": None,
    "episode": "s01e01",
    "t": 12.33,
}
BATCH = [ITEM, {**ITEM, "id": "102", "lemma": "cop", "surface": "cop", "ipa": "kɒp"}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """socket 地雷：本文件任何用例只要碰网络就当场失败。"""

    class Boom(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError("provider 测试不许发起任何网络调用")

    monkeypatch.setattr(socket, "socket", Boom)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    """确保测试环境里没有真 key（否则骨架用例会想去发请求）。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_price_override(monkeypatch):
    """牌价环境变量清干净：本机若设过覆盖值，估价类用例会莫名其妙地飘。"""
    for prefix in ("POI_DEEPSEEK_PRICE", "POI_ANTHROPIC_PRICE"):
        for suffix in ("_IN", "_OUT"):
            monkeypatch.delenv(prefix + suffix, raising=False)


# --- 注册表 ----------------------------------------------------------------


def test_registry_has_three_builtins():
    assert set(provider_names()) >= {"fake", "anthropic", "deepseek"}


def test_get_provider_is_case_insensitive_and_trims():
    assert get_provider("  FAKE ").name == "fake"


def test_get_provider_unknown_raises_with_hint():
    with pytest.raises(ProviderError) as e:
        get_provider("gpt-9")
    assert "fake" in str(e.value)


def test_provider_protocol_shape():
    p = get_provider("fake")
    assert callable(p.annotate) and callable(p.estimate_cost)


# --- fake provider ---------------------------------------------------------


def test_fake_is_deterministic_and_free():
    p, q = FakeProvider(), FakeProvider()
    assert p.annotate(BATCH) == q.annotate(BATCH)
    assert p.estimate_cost(BATCH) == 0.0


def test_fake_output_shape_follows_design_contract():
    out = FakeProvider().annotate([ITEM])[0]
    validate_annotation(out)
    assert out["context_gloss"].startswith("〔fake〕")
    assert "盯梢；监视" in out["context_gloss"]
    assert ITEM["sentence"][:20] in out["context_gloss"]
    morph = next(h for h in out["hooks"] if h["type"] == "morph")
    assert has_disclaimer(morph["label"])  # DESIGN §5：拆分永远标未经核验
    assert "核验" in morph["label"]


def test_fake_pun_hook_only_when_ipa_present():
    with_ipa = FakeProvider().annotate([ITEM])[0]
    without = FakeProvider().annotate([{**ITEM, "ipa": None}])[0]
    assert any(h["type"] == "pun" for h in with_ipa["hooks"])
    assert all(h["type"] != "pun" for h in without["hooks"])


def test_fake_handles_missing_fields():
    out = FakeProvider().annotate([{"lemma": "x"}])[0]
    validate_annotation(out)
    assert "（词典未收录）" in out["context_gloss"]


def test_fake_fail_on_always():
    p = FakeProvider(fail_on="stakeout")
    for _ in range(3):
        with pytest.raises(FakeInjectedFailure):
            p.annotate([ITEM])
    assert p.fail_count == 3
    p.annotate([{**ITEM, "lemma": "other", "surface": "other"}])  # 别的词不受影响


def test_fake_fail_times_then_success():
    p = FakeProvider(fail_on="stakeout", fail_times=1)
    with pytest.raises(FakeInjectedFailure):
        p.annotate([ITEM])
    assert p.annotate([ITEM])[0]["context_gloss"].startswith("〔fake〕")


def test_fake_bad_output_and_injectable_cost():
    bad = FakeProvider(bad_output_on="stakeout").annotate([ITEM])[0]
    with pytest.raises(SchemaViolation):
        validate_annotation(bad)
    assert FakeProvider(cost_per_item=1.5).estimate_cost(BATCH) == 3.0


def test_fake_echoes_the_input_id():
    out = FakeProvider().annotate(BATCH)
    assert [o["id"] for o in out] == ["101", "102"]


def test_fake_shuffle_and_bogus_id_injections():
    shuffled = FakeProvider(shuffle=True).annotate(BATCH)
    assert [o["id"] for o in shuffled] == ["102", "101"]  # 顺序乱了，id 还在
    wrong = FakeProvider(wrong_id_on="stakeout").annotate(BATCH)
    assert {o["id"] for o in wrong} == {"101-bogus", "102"}
    dropped = FakeProvider(drop_id_on="stakeout").annotate(BATCH)
    assert "id" not in dropped[0] and dropped[1]["id"] == "102"
    dup = FakeProvider(duplicate_id_on="stakeout").annotate(BATCH)
    assert [o["id"] for o in dup] == ["101", "102", "101"]


def test_fake_falls_back_to_lemma_when_pack_has_no_id():
    out = FakeProvider().annotate([{"lemma": "x"}])[0]
    assert out["id"] == "x"
    validate_annotation(out)


def test_fake_records_calls_for_retry_assertions():
    p = FakeProvider()
    p.annotate([ITEM])
    p.annotate(BATCH)
    assert [len(c) for c in p.calls] == [1, 2]


# --- schema 校验 -----------------------------------------------------------

GOOD = {"id": "101", "context_gloss": "（这句里）临时被叫去执行的盯梢任务", "hooks": []}

BAD_CASES = {
    "非对象": ["nope"],
    "缺 id": {"context_gloss": "x", "hooks": []},
    "id 非字符串": {"id": 101, "context_gloss": "x", "hooks": []},
    "id 为空": {"id": "", "context_gloss": "x", "hooks": []},
    "缺 context_gloss": {"id": "1", "hooks": []},
    "缺 hooks": {"id": "1", "context_gloss": "x"},
    "空 gloss": {"id": "1", "context_gloss": "", "hooks": []},
    "gloss 非字符串": {"id": "1", "context_gloss": 42, "hooks": []},
    "hooks 非数组": {"id": "1", "context_gloss": "x", "hooks": "morph"},
    "多出字段": {"id": "1", "context_gloss": "x", "hooks": [], "factual": "我很确信"},
    "hook 非对象": {"id": "1", "context_gloss": "x", "hooks": ["morph"]},
    "hook 缺 label": {
        "id": "1", "context_gloss": "x", "hooks": [{"type": "morph", "text": "t"}]
    },
    "hook type 非法": {
        "id": "1",
        "context_gloss": "x",
        "hooks": [{"type": "Morph 拆分", "text": "t", "label": "l"}],
    },
    "hook text 为空": {
        "id": "1",
        "context_gloss": "x",
        "hooks": [{"type": "morph", "text": "", "label": "l"}],
    },
    "hook 多出字段": {
        "id": "1",
        "context_gloss": "x",
        "hooks": [{"type": "morph", "text": "t", "label": "l", "confidence": 0.9}],
    },
}


def test_schema_accepts_design_example():
    example = {
        "id": "101",
        "context_gloss": "（这句里）临时被叫去执行的盯梢任务",
        "hooks": [
            {"type": "morph", "text": "stake 桩 + out 在外", "label": "拆分助记，未经词源核验"},
            {"type": "pun", "text": "死盯凯特", "label": "记忆钩子，非词源"},
        ],
    }
    assert validate_annotation(example) is example


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_schema_rejects_malformed(name):
    with pytest.raises(SchemaViolation):
        validate_annotation(BAD_CASES[name])


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_manual_validator_agrees_with_jsonschema(name):
    """jsonschema 缺席时的手写兜底必须给出一致判断。"""
    with pytest.raises(SchemaViolation):
        _manual_validate(BAD_CASES[name])
    _manual_validate(GOOD)


# --- 按 id 对位（工单 6-2） -------------------------------------------------


def test_coerce_id_accepts_str_and_integral_numbers():
    assert coerce_id("17") == "17" and coerce_id(" 17 ") == "17"
    assert coerce_id(17) == "17" and coerce_id(17.0) == "17"
    for bad in (None, True, False, 1.5, "", " ", "x" * 65, [], {}):
        assert coerce_id(bad) is None


def test_pack_id_falls_back_to_lemma():
    assert pack_id({"id": "7", "lemma": "cop"}) == "7"
    assert pack_id({"lemma": "cop"}) == "cop"
    assert pack_id({"surface": "cops"}) == "cops"


def test_match_annotations_is_order_independent():
    matched, problems = match_annotations(
        BATCH,
        [
            {"id": "102", "context_gloss": "警察", "hooks": []},
            {"id": 101, "context_gloss": "盯梢", "hooks": []},  # 数字 id 也认
        ],
    )
    assert problems == []
    assert matched["101"]["context_gloss"] == "盯梢"
    assert matched["102"]["context_gloss"] == "警察"


@pytest.mark.parametrize(
    "items,kept",
    [
        ([{"context_gloss": "g", "hooks": []}], []),                      # 缺 id
        ([{"id": "9", "context_gloss": "g", "hooks": []}], []),           # 不在本批
        (["不是对象"], []),
        ([{"id": "101", "context_gloss": "", "hooks": []}], []),          # 不合 schema
        ([{"id": "101", "context_gloss": "a", "hooks": []},
          {"id": "101", "context_gloss": "b", "hooks": []}], []),         # 重复 → 全丢
        ([{"id": "101", "context_gloss": "a", "hooks": []},
          {"id": "101", "context_gloss": "b", "hooks": []},
          {"id": "102", "context_gloss": "c", "hooks": []}], ["102"]),
    ],
)
def test_match_annotations_discards_unmatchable(items, kept):
    matched, problems = match_annotations(BATCH, items)
    assert sorted(matched) == kept and problems


def test_match_annotations_rejects_non_list():
    matched, problems = match_annotations(BATCH, {"id": "101"})
    assert matched == {} and problems


def test_match_annotations_enforces_labels():
    matched, _ = match_annotations(
        BATCH,
        [{"id": "101", "context_gloss": "g",
          "hooks": [{"type": "morph", "text": "t", "label": "词源考据"}]}],
    )
    assert matched["101"]["hooks"][0]["label"] == MORPH_LABEL


# --- 免责标签兜底 -----------------------------------------------------------


def test_enforce_labels_fixes_morph_label():
    out = enforce_labels(
        {"id": "1", "context_gloss": "x",
         "hooks": [{"type": "morph", "text": "t", "label": "词源"}]}
    )
    assert out["hooks"][0]["label"] == MORPH_LABEL
    assert out["id"] == "1"  # id 原样保留，落库前才被丢掉


def test_enforce_labels_marks_other_hooks_non_etymological():
    out = enforce_labels(
        {"id": "1", "context_gloss": "x",
         "hooks": [{"type": "pun", "text": "t", "label": "谐音"}]}
    )
    assert GENERIC_DISCLAIMER in out["hooks"][0]["label"]


def test_enforce_labels_keeps_good_labels_and_does_not_mutate():
    src = {
        "id": "1",
        "context_gloss": "x",
        "hooks": [{"type": "pun", "text": "t", "label": "记忆钩子，非词源"}],
    }
    out = enforce_labels(src)
    assert out["hooks"][0]["label"] == "记忆钩子，非词源"
    out["hooks"][0]["label"] = "改了"
    assert src["hooks"][0]["label"] == "记忆钩子，非词源"


# --- 提示词 ----------------------------------------------------------------


def test_prompt_feeds_ipa_for_pun_hooks():
    """音标进 prompt 做谐音钩子——这是用户点名要的（工单 3）。"""
    prompt = build_user_prompt([ITEM])
    assert "ˈsteɪkaʊt" in prompt
    assert "ipa" in prompt and "谐音" in prompt


def test_prompt_states_output_rules():
    from app.providers.prompts import SYSTEM_PROMPT

    assert "宁缺毋滥" in SYSTEM_PROMPT
    assert "非词源" in SYSTEM_PROMPT
    assert "未经词源核验" in SYSTEM_PROMPT  # morph 永远标未经核验
    assert "词源" in SYSTEM_PROMPT and "不许" in SYSTEM_PROMPT


def test_prompt_demands_id_echo():
    """对位靠 id：id 必须进 prompt，且明确要求原样回传（工单 6-2）。"""
    from app.providers.prompts import SYSTEM_PROMPT

    prompt = build_user_prompt(BATCH)
    assert "- id: 101" in prompt and "- id: 102" in prompt
    assert "原样回传" in prompt
    assert '"101", "102"' in prompt  # 结尾复述一遍本批 id
    assert "id" in SYSTEM_PROMPT and "原样抄回" in SYSTEM_PROMPT


def test_prompt_carries_every_item_and_count():
    prompt = build_user_prompt(BATCH)
    assert prompt.count("### 条目") == 2
    assert "长度为 2" in prompt or "长度 2" in prompt
    assert "cop" in prompt and "stakeout" in prompt


def test_prompt_renders_missing_fields_as_placeholder():
    prompt = build_user_prompt([{"lemma": "x"}])
    assert "（无）" in prompt and "x" in prompt


def test_debug_dump_contains_system_and_input():
    dump = debug_dump([ITEM])
    assert "=== SYSTEM ===" in dump and "=== USER ===" in dump and "stakeout" in dump


# --- 响应解析 --------------------------------------------------------------


def test_parse_json_array_variants():
    assert parse_json_array('[{"a":1}]') == [{"a": 1}]
    assert parse_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert parse_json_array('好的：\n[{"a":1}]\n希望有用') == [{"a": 1}]
    assert parse_json_array('{"items":[{"a":1}]}') == [{"a": 1}]  # json_object 模式
    assert parse_json_array('{"context_gloss":"g","hooks":[]}') == [
        {"context_gloss": "g", "hooks": []}
    ]


@pytest.mark.parametrize("text", ["", "   ", "没有 JSON", "[不是合法 json"])
def test_parse_json_array_rejects_garbage(text):
    with pytest.raises(SchemaViolation):
        parse_json_array(text)


def test_approx_tokens_counts_cjk_heavier():
    assert approx_tokens("") == 0
    assert approx_tokens("盯梢监视") > approx_tokens("abcd")


# --- 真 provider 骨架（无 key，绝不发包） ----------------------------------


@pytest.mark.parametrize("name,env", [("anthropic", "ANTHROPIC_API_KEY"),
                                      ("deepseek", "DEEPSEEK_API_KEY")])
def test_real_provider_raises_not_configured_without_key(name, env):
    p = get_provider(name)
    assert p.configured is False
    with pytest.raises(ProviderNotConfigured) as e:
        p.annotate(BATCH)
    assert env in str(e.value)


@pytest.mark.parametrize("name", ["anthropic", "deepseek"])
def test_real_provider_assembles_prompt_and_cost_offline(name):
    """缺 key 也能拼 prompt、算预算——离线调提示词的前提。"""
    p = get_provider(name)
    payload = p.payload(BATCH)
    blob = str(payload)
    assert p.model and "ˈsteɪkaʊt" in blob and "kɒp" in blob
    cost = p.estimate_cost(BATCH)
    assert cost > 0 and p.estimate_cost([]) == 0.0


def test_anthropic_payload_shape():
    p = get_provider("anthropic", api_key="k")
    payload = p.payload(BATCH)
    assert payload["messages"][-1] == {"role": "assistant", "content": "["}
    assert "system" in payload and payload["max_tokens"] > 0
    assert p.headers()["x-api-key"] == "k"


def test_deepseek_payload_shape():
    p = get_provider("deepseek", api_key="k")
    payload = p.payload(BATCH)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["role"] == "system"
    assert "items" in payload["messages"][1]["content"]
    assert p.headers()["authorization"] == "Bearer k"


# --- 工单 8a：v4-flash 迁移 + 关 thinking + --model 覆盖（全程零网络） -------


def test_deepseek_defaults_to_v4_flash():
    """旧的 deepseek-chat / deepseek-reasoner 已退役，默认必须是 v4-flash。"""
    assert DS_DEFAULT_MODEL == "deepseek-v4-flash"
    p = get_provider("deepseek", api_key="k")
    assert p.model == "deepseek-v4-flash"
    assert p.payload(BATCH)["model"] == "deepseek-v4-flash"


def test_deepseek_disables_thinking_explicitly():
    """v4 系列默认 thinking 开着且 effort=high，不关会重度多计费（工单 8a）。"""
    payload = get_provider("deepseek", api_key="k").payload(BATCH)
    assert payload["thinking"] == {"type": "disabled"}


def test_deepseek_model_override_wins():
    p = get_provider("deepseek", api_key="k", model="deepseek-v4")
    assert p.model == "deepseek-v4"
    assert p.payload(BATCH)["model"] == "deepseek-v4"
    # 覆盖不许污染类属性：下一个默认实例还是 v4-flash
    assert get_provider("deepseek", api_key="k").model == "deepseek-v4-flash"


def test_deepseek_model_override_reaches_transport():
    """model= 的透传终点是请求体本身（注入 transport 拦下来看，零真实网络）。"""
    seen: list[dict] = []

    def transport(payload: dict) -> dict:
        seen.append(payload)
        return _resp('[{"id":"101","context_gloss":"g","hooks":[]}]', "deepseek")

    p = get_provider(
        "deepseek", api_key="k", model="deepseek-v4-flash-2512", transport=transport
    )
    p.annotate([ITEM])
    assert seen[0]["model"] == "deepseek-v4-flash-2512"
    assert seen[0]["thinking"] == {"type": "disabled"}


def test_deepseek_price_table_is_cache_miss(monkeypatch):
    """官方人民币牌价（工单 17-4 修正）：命中 ¥0.02 / 未命中 ¥1 / 输出 ¥2，无峰谷。"""
    import app.providers.deepseek as DS

    assert DS.PRICE_IN_HIT_CNY_PER_MTOK == 0.02
    assert DS.PRICE_IN_MISS_CNY_PER_MTOK == 1.0
    assert DS.PRICE_OUT_CNY_PER_MTOK == 2.0
    assert DS.REFERENCE_PRICES_CNY_PER_MTOK == {"in_miss": 1.0, "in_hit": 0.02, "out": 2.0}

    p = get_provider("deepseek")
    # 估价用的就是 cache-miss 输入 + 输出这两个数，不掺汇率、不打折
    assert p.price_in_cny_per_mtok == DS.PRICE_IN_MISS_CNY_PER_MTOK
    assert p.price_out_cny_per_mtok == DS.PRICE_OUT_CNY_PER_MTOK
    # 命中缓存比估价便宜：估价确实是上界
    assert DS.PRICE_IN_HIT_CNY_PER_MTOK < p.price_in_cny_per_mtok


def test_deepseek_price_table_has_no_peak_window_anymore():
    """DeepSeek 没有峰时/错峰档：那套常量和时段叙述都不许再出现（工单 17-4）。"""
    import app.providers.deepseek as DS

    for gone in (
        "PRICE_IN_MISS_PEAK_CNY_PER_MTOK",
        "PRICE_OUT_PEAK_CNY_PER_MTOK",
        "PRICE_IN_MISS_OFFPEAK_CNY_PER_MTOK",
        "PRICE_OUT_OFFPEAK_CNY_PER_MTOK",
        "PRICE_IN_HIT_PEAK_CNY_PER_MTOK",
        "PRICE_IN_HIT_OFFPEAK_CNY_PER_MTOK",
        "PEAK_HOURS_BEIJING",
    ):
        assert not hasattr(DS, gone), f"{gone} 是错价目的残留，该删"
    # 连叙述都不许留：模块里再没有"峰时/错峰/北京时间"这类时段档位的说法
    src = Path(DS.__file__).read_text(encoding="utf-8")
    for word in ("峰时", "错峰", "峰价", "北京时间"):
        assert word not in src, f"价目叙述里还留着「{word}」"


def test_deepseek_estimate_is_sane_per_word():
    """一批 4 词（默认 batch）每词约 ¥0.001；离线可算，不发请求。

    比工单 17-4 之前便宜了一截 —— 那时按错的 ¥3/¥9 估，现在按 ¥1/¥2。
    """
    batch = [{**ITEM, "id": str(i)} for i in range(4)]
    per_word = get_provider("deepseek").estimate_cost(batch) / 4
    assert 0.0005 < per_word < 0.002


# --- 工单 16-2：牌价 = 带元数据的结构 + 可覆盖 + 非法即 fail closed ---------


@pytest.mark.parametrize("name", ["deepseek", "anthropic"])
def test_price_is_a_structure_with_metadata(name):
    """两家 provider 的牌价**同一个形状**：单价 + 币种 + 来源 + as_of + 备注。"""
    p = get_provider(name)
    price = p.base_price()
    assert isinstance(price, Price)
    assert price.input_per_mtok > 0 and price.output_per_mtok > 0
    assert price.currency == "CNY" and price.symbol == "¥"
    assert price.source and price.as_of and price.note
    assert "以官方现价为准" in price.note
    # 结构能整体导出（对账/日志用），键名两家一致
    assert set(price.as_dict()) == {
        "input_per_mtok", "output_per_mtok", "currency", "source", "as_of", "note",
    }


def test_price_values_unchanged_by_the_refactor():
    """结构化只是换了个装法，数值一分没动。"""
    import app.providers.anthropic_api as AN
    import app.providers.deepseek as DS

    assert (DS.PRICE.input_per_mtok, DS.PRICE.output_per_mtok) == (1.0, 2.0)
    assert DS.PRICE.as_of == "2026-08-19"
    assert (AN.PRICE.input_per_mtok, AN.PRICE.output_per_mtok) == (7.2, 36.0)
    # Anthropic 那两个数是美元牌价折算的粗估、没核过现价：as_of 就得说实话
    assert "unverified" in AN.PRICE.as_of and "非" in AN.PRICE.source
    # 老名字仍然可用（README / 外部代码引用的是它们）
    assert get_provider("deepseek").price_in_cny_per_mtok == 1.0
    assert get_provider("anthropic").price_out_cny_per_mtok == 36.0


def test_estimate_cost_carries_as_of_but_is_still_a_float():
    """估价结果自带 as_of 标注，同时对老调用方完全是个 float。"""
    est = get_provider("deepseek").estimate_cost(BATCH)
    assert isinstance(est, float) and est > 0
    assert est.as_of == "2026-08-19"
    assert est.price.currency == "CNY"
    assert "as_of=2026-08-19" in est.label()
    assert "as_of=2026-08-19" in repr(est)
    # 预算比较 / 取整 / 格式化 一律照旧
    assert round(est, 4) == round(float(est), 4)
    assert (est > 999.0) is False and f"{est:.4f}".count(".") == 1
    assert get_provider("deepseek").estimate_cost([]) == 0.0


def test_price_label_shows_source_and_as_of():
    label = get_provider("deepseek").price_label()
    assert "¥1" in label and "¥2" in label
    assert "as_of=2026-08-19" in label and "official price page" in label
    assert get_provider("deepseek").resolved_price().stamp() == "牌价 as_of=2026-08-19"


def test_price_env_override_changes_the_estimate(monkeypatch):
    """牌价变了不必改代码：POI_DEEPSEEK_PRICE_IN/OUT 直接覆盖（元 / 百万 token）。"""
    base = get_provider("deepseek").estimate_cost(BATCH)
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", "10")
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", "20")
    p = get_provider("deepseek")
    assert p.price_in_cny_per_mtok == 10.0 and p.price_out_cny_per_mtok == 20.0
    est = p.estimate_cost(BATCH)
    assert est == pytest.approx(float(base) * 10, rel=1e-3)
    # 覆盖后 as_of / source 要说清"这不是表里那份"
    assert "env-override" in est.as_of and "2026-08-19" in est.as_of
    assert "POI_DEEPSEEK_PRICE_IN+POI_DEEPSEEK_PRICE_OUT" in est.price.source


def test_price_env_override_can_be_partial(monkeypatch):
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", "1.5")
    price = get_provider("deepseek").resolved_price()
    assert price.input_per_mtok == 1.0 and price.output_per_mtok == 1.5
    assert "POI_DEEPSEEK_PRICE_OUT" in price.source
    assert "POI_DEEPSEEK_PRICE_IN+" not in price.source


def test_price_env_override_zero_is_allowed(monkeypatch):
    """0 是合法价（免费档），不当异常处理——只有负数/NaN/非数才算坏。"""
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", "0")
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", "0")
    assert get_provider("deepseek").estimate_cost(BATCH) == 0.0


@pytest.mark.parametrize(
    "bad", ["-1", "-0.001", "nan", "NaN", "inf", "-inf", "abc", "3.0元", "", "  "]
)
def test_illegal_price_override_fails_closed(monkeypatch, bad):
    """非法覆盖值 = 估价失败，绝不"当没看见"按原价照跑。"""
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", bad)
    p = get_provider("deepseek")
    with pytest.raises(PriceConfigError) as e:
        p.estimate_cost(BATCH)
    assert "POI_DEEPSEEK_PRICE_IN" in str(e.value)
    with pytest.raises(PriceConfigError):  # 老属性同样拦，不给任何后门
        p.price_in_cny_per_mtok


def test_illegal_price_override_on_output_side(monkeypatch):
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_OUT", "-5")
    with pytest.raises(PriceConfigError, match="POI_DEEPSEEK_PRICE_OUT"):
        get_provider("deepseek").estimate_cost(BATCH)


def test_each_provider_reads_its_own_env_prefix(monkeypatch):
    """别串味：设 DeepSeek 的覆盖不许影响 Anthropic，反之亦然。"""
    monkeypatch.setenv("POI_DEEPSEEK_PRICE_IN", "99")
    assert get_provider("anthropic").price_in_cny_per_mtok == 7.2
    monkeypatch.delenv("POI_DEEPSEEK_PRICE_IN")
    monkeypatch.setenv("POI_ANTHROPIC_PRICE_IN", "99")
    assert get_provider("anthropic").price_in_cny_per_mtok == 99.0
    assert get_provider("deepseek").price_in_cny_per_mtok == 1.0


def test_provider_without_price_fails_closed():
    """自定义 provider 忘了填牌价：估价直接抛，不许按 ¥0 蒙混过关。"""

    class Unpriced(ChatJSONProvider):
        name = "unpriced"

    with pytest.raises(PriceConfigError, match="没有牌价"):
        Unpriced().estimate_cost(BATCH)


def test_legacy_float_price_attributes_still_work():
    """老式写法（两个 float 类属性）仍然能估价，只是 as_of 标"未标注"。"""

    class Legacy(ChatJSONProvider):
        name = "legacy"
        price_in_cny_per_mtok = 3.0
        price_out_cny_per_mtok = 9.0

    class Structured(ChatJSONProvider):
        name = "structured"
        price = Price(3.0, 9.0, as_of="2026-08-16")

    est = Legacy().estimate_cost(BATCH)
    assert est == pytest.approx(float(Structured().estimate_cost(BATCH)))
    assert est.price.input_per_mtok == 3.0 and est.as_of == "未标注"


def test_legacy_float_price_attributes_are_validated():
    class BadLegacy(ChatJSONProvider):
        name = "bad-legacy"
        price_in_cny_per_mtok = -1.0
        price_out_cny_per_mtok = 9.0

    with pytest.raises(PriceConfigError, match="负数"):
        BadLegacy().estimate_cost(BATCH)


def _resp(text: str, name: str) -> dict:
    if name == "anthropic":
        return {"content": [{"type": "text", "text": text}]}
    return {"choices": [{"message": {"content": text}}]}


@pytest.mark.parametrize("name", ["anthropic", "deepseek"])
def test_real_provider_happy_path_with_injected_transport(name):
    """注入假传输层跑完整链路：组装 → 解析 → 按 id 对位 → 校验 → 补标签。"""
    body = (
        '[{"id":"101","context_gloss":"（这句里）临时盯梢任务","hooks":'
        '[{"type":"morph","text":"stake+out","label":"忘了写免责"}]},'
        '{"id":"102","context_gloss":"警察（口语）","hooks":[]}]'
    )
    if name == "anthropic":  # 预填 "[" 后模型只会吐剩下的部分
        body = body[1:]
    p = get_provider(name, api_key="k", transport=lambda payload: _resp(body, name))
    out = p.annotate(BATCH)
    assert len(out) == 2
    assert out[0]["hooks"][0]["label"] == MORPH_LABEL  # 代码兜底补上免责标签
    assert out[1]["hooks"] == []


@pytest.mark.parametrize("name", ["anthropic", "deepseek"])
def test_real_provider_matches_by_id_not_by_order(name):
    """模型把数组顺序调了个个儿：仍然必须按 id 对回去（工单 6-2）。"""
    body = (
        '[{"id":"102","context_gloss":"警察（口语）","hooks":[]},'
        '{"id":"101","context_gloss":"临时盯梢任务","hooks":[]}]'
    )
    if name == "anthropic":
        body = body[1:]
    p = get_provider(name, api_key="k", transport=lambda payload: _resp(body, name))
    out = p.annotate(BATCH)
    got = {o["id"]: o["context_gloss"] for o in out}
    assert got == {"101": "临时盯梢任务", "102": "警察（口语）"}


def test_real_provider_drops_elements_that_cannot_be_matched():
    """缺 id / id 是模型自己编的 → 丢弃，只吐对上号的那些（剩下的 worker 重试）。"""
    body = (
        '[{"id":"101","context_gloss":"临时盯梢任务","hooks":[]},'
        '{"id":"999","context_gloss":"这条 id 不在本批","hooks":[]},'
        '{"context_gloss":"这条没有 id","hooks":[]}]'
    )
    p = get_provider(
        "deepseek", api_key="k", transport=lambda _p: _resp(body, "deepseek"), retries=0
    )
    out = p.annotate(BATCH)
    assert [o["id"] for o in out] == ["101"]
    assert len(p.last_problems) == 2


def test_real_provider_drops_both_copies_of_duplicated_id():
    """id 重复：两条至少有一条张冠李戴，两条都不要。"""
    body = (
        '[{"id":"101","context_gloss":"第一条","hooks":[]},'
        '{"id":"101","context_gloss":"第二条","hooks":[]},'
        '{"id":"102","context_gloss":"警察","hooks":[]}]'
    )
    p = get_provider(
        "deepseek", api_key="k", transport=lambda _p: _resp(body, "deepseek"), retries=0
    )
    out = p.annotate(BATCH)
    assert [o["id"] for o in out] == ["102"]


def test_real_provider_raises_when_nothing_matches():
    """一条都对不上 → SchemaViolation（可重试）。"""
    body = '[{"id":"777","context_gloss":"g","hooks":[]}]'
    p = get_provider(
        "deepseek", api_key="k", transport=lambda _p: _resp(body, "deepseek"), retries=0
    )
    with pytest.raises(SchemaViolation) as e:
        p.annotate(BATCH)
    assert "id" in str(e.value)


def test_real_provider_retries_transient_then_succeeds():
    calls = {"n": 0}

    def transport(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderTransientError("429 慢点")
        return _resp('[{"id":"101","context_gloss":"g","hooks":[]}]', "deepseek")

    p = get_provider("deepseek", api_key="k", transport=transport, sleep=lambda _s: None)
    assert p.annotate([ITEM])[0]["context_gloss"] == "g"
    assert calls["n"] == 2


def test_real_provider_gives_up_after_retries():
    calls = {"n": 0}

    def transport(payload):
        calls["n"] += 1
        raise ProviderTransientError("503")

    p = get_provider(
        "deepseek", api_key="k", transport=transport, retries=2, sleep=lambda _s: None
    )
    with pytest.raises(ProviderTransientError):
        p.annotate([ITEM])
    assert calls["n"] == 3  # 1 次 + 重试 2 次


def test_real_provider_short_output_returns_only_matched():
    """2 进 1 出不再是错误：对上号的照收，没对上的那条留给 worker 下一轮重试。"""
    p = get_provider(
        "deepseek",
        api_key="k",
        transport=lambda _p: _resp(
            '[{"id":"102","context_gloss":"g","hooks":[]}]', "deepseek"
        ),
        retries=0,
    )
    out = p.annotate(BATCH)
    assert [o["id"] for o in out] == ["102"]


def test_real_provider_empty_batch_short_circuits():
    p = get_provider("deepseek")  # 没 key 也不该抛：空批次根本不发请求
    assert p.annotate([]) == []
