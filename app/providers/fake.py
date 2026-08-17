"""确定性假 provider：测试与离线开发用，永远 0 元。

同样的输入永远得到同样的输出（没有随机、没有时间戳），所以可以直接断言字符串。
所有内容都打着「〔fake〕」前缀，一眼看得出不是真货。

可注入的故障模式（给 worker 的重试/失败/预算路径当靶子）：

    FakeProvider(fail_on="stakeout")              # 该词永远让整批调用抛异常
    FakeProvider(fail_on="stakeout", fail_times=1)# 前 1 次抛，第 2 次成功（测重试成功）
    FakeProvider(bad_output_on="stakeout")        # 返回畸形 JSON（测 schema 拒收）
    FakeProvider(cost_per_item=1.5)               # 估价非 0（测预算截断）
    FakeProvider(shuffle=True)                    # 输出数组倒序（测 id 对位）
    FakeProvider(wrong_id_on="stakeout")          # 该词的 id 写成别的（测丢弃+重试）
    FakeProvider(drop_id_on="stakeout")           # 该词不带 id（测丢弃+重试）
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from app.providers import GENERIC_LABEL, MORPH_LABEL, ProviderError, pack_id

GLOSS_PREFIX = "〔fake〕"
SENTENCE_CLIP = 20


class FakeInjectedFailure(ProviderError):
    """fail_on 注入的假故障（可重试类，worker 会按重试策略处理）。"""


def _as_set(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(value)


class FakeProvider:
    """按模板生成合规输出的假 provider。"""

    name = "fake"

    def __init__(
        self,
        fail_on: str | Iterable[str] | None = None,
        fail_times: int | None = None,
        bad_output_on: str | Iterable[str] | None = None,
        cost_per_item: float = 0.0,
        hooks_for: Callable[[dict], list[dict]] | None = None,
        shuffle: bool = False,
        wrong_id_on: str | Iterable[str] | None = None,
        drop_id_on: str | Iterable[str] | None = None,
        duplicate_id_on: str | Iterable[str] | None = None,
    ) -> None:
        """
        fail_on / bad_output_on / wrong_id_on / drop_id_on 按 lemma 匹配（也认 surface）。
        fail_times=None 表示永远失败；给整数则只失败前 N 次调用。
        cost_per_item 默认 0——假 provider 不花钱是它存在的意义。
        shuffle=True 把输出数组倒序吐出来（确定性的"乱序"，模拟模型重排）。
        """
        self.fail_on = _as_set(fail_on)
        self.fail_times = fail_times
        self.bad_output_on = _as_set(bad_output_on)
        self.cost_per_item = float(cost_per_item)
        self.hooks_for = hooks_for
        self.shuffle = bool(shuffle)
        self.wrong_id_on = _as_set(wrong_id_on)
        self.drop_id_on = _as_set(drop_id_on)
        self.duplicate_id_on = _as_set(duplicate_id_on)
        # 观测用：测试靠这些断言重试次数
        self.calls: list[list[dict]] = []
        self.fail_count = 0

    # --- 协议 --------------------------------------------------------------

    def annotate(self, batch: list[dict]) -> list[dict]:
        self.calls.append([dict(i) for i in batch])
        self._maybe_fail(batch)
        outs = [self._one(item) for item in batch]
        if self.duplicate_id_on:
            dup = [
                dict(o, id=pack_id(item))
                for item, o in zip(batch, outs)
                if self._keys(item) & self.duplicate_id_on
            ]
            outs.extend(dup)
        if self.shuffle:  # 确定性"乱序"：倒序。顺序无语义，解析侧靠 id 对位
            outs.reverse()
        return outs

    def estimate_cost(self, batch: list[dict]) -> float:
        return round(self.cost_per_item * len(batch), 6)

    # --- 内部 --------------------------------------------------------------

    def _keys(self, item: dict) -> set[str]:
        return {str(item.get("lemma") or ""), str(item.get("surface") or "")}

    def _maybe_fail(self, batch: list[dict]) -> None:
        if not self.fail_on:
            return
        hit = next((i for i in batch if self._keys(i) & self.fail_on), None)
        if hit is None:
            return
        if self.fail_times is not None and self.fail_count >= self.fail_times:
            return
        self.fail_count += 1
        raise FakeInjectedFailure(
            f"注入故障: lemma={hit.get('lemma')!r}（第 {self.fail_count} 次）"
        )

    def _one(self, item: dict) -> dict[str, Any]:
        lemma = str(item.get("lemma") or item.get("surface") or "?")
        rid = pack_id(item)  # 真 provider 由模型抄回，假 provider 直接抄
        if self._keys(item) & self.bad_output_on:
            # 畸形输出：hooks 是字符串、还多一个字段 —— schema 必须拒收
            return {"id": rid, "context_gloss": "", "hooks": "morph", "factual": "我很确信"}

        dict_gloss = str(item.get("dict_gloss") or "（词典未收录）")
        sentence = str(item.get("sentence") or "")
        gloss = f"{GLOSS_PREFIX}{dict_gloss} · {sentence[:SENTENCE_CLIP]}"

        if self.hooks_for is not None:
            hooks = self.hooks_for(item)
        else:
            head, tail = lemma[: max(1, len(lemma) // 2)], lemma[max(1, len(lemma) // 2) :]
            hooks = [
                {
                    "type": "morph",
                    "text": f"{head} + {tail}——{GLOSS_PREFIX}拆分示例，仅供占位",
                    "label": MORPH_LABEL,
                }
            ]
            ipa = item.get("ipa")
            if ipa:
                hooks.append(
                    {
                        "type": "pun",
                        "text": f"读音 /{ipa}/——{GLOSS_PREFIX}谐音占位",
                        "label": GENERIC_LABEL,
                    }
                )
        out: dict[str, Any] = {"id": rid, "context_gloss": gloss[:400], "hooks": hooks}
        if self._keys(item) & self.wrong_id_on:
            out["id"] = f"{rid}-bogus"  # 模型编了个本批没有的 id
        elif self._keys(item) & self.drop_id_on:
            out.pop("id")  # 模型压根忘了带 id
        return out
