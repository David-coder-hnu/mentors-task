"""决策体 —— schema 校验、缓存接线、状态规范化（T5）。

设计方案 `:1403` 给 T5 的 Verify 有两条，这里是它们各自的落点：

| Verify 原话 | 在哪儿 |
|---|---|
| 「改 prompt 内容 ⇒ 缓存未命中」 | `TestCacheWiring`（键那一层在 `test_decision_cache.py`） |
| 「schema 校验拒绝畸形输出」 | `TestParseAction` |
| v6(b)「同一个键在两个 `PYTHONHASHSEED` 下必须相等」 | `tests/unit/test_canonical.py` / `test_decision_cache.py` |

另外两件事这个文件要钉住，它们**不在 Verify 的原话里但属同一个交付物**：

* **`Stage1State` 与回包到达顺序无关**（`:564`，bug 2）——`TestStateIsOrderIndependent`；
* **畸形输出也必须进缓存**——`TestMalformedOutputIsCached`。理由见那个类的 docstring：
  不缓存它，回放就会重新问一次模型，于是"复现"复现出另一条轨迹。
"""

from __future__ import annotations

import pytest

from src.agent.cache import CacheKey, DecisionCache
from src.agent.canonical import canonical
from src.agent.decide import (
    LLM_IDENTITY_KEYS,
    DecisionError,
    MalformedDecision,
    Stage1State,
    Tool,
    ToolPool,
    decide,
    parse_action,
)

PREFIX = "2001:db8:dead::/64"
OTHER_PREFIX = "2001:db8:cafe::/64"

CONFIG = {
    "model": "claude-sonnet-5-5",
    "model_version": "2026-09-01",
    "temperature": 0.0,
    "top_p": 1.0,
    "max_tokens": 1024,
    "call_date": "2026-10-09",
}


def make_pool(*extra: Tool) -> ToolPool:
    return ToolPool(
        [
            Tool("probe", "向一个地址发一次探测"),
            Tool("report", "提交某个前缀是否 aliased 的判定"),
            *extra,
        ]
    )


def make_state(*, prefixes: tuple[str, ...] = (PREFIX,), budget: int = 1000) -> Stage1State:
    return Stage1State.initial(prefixes=prefixes, budget=budget)


class CountingCompletion:
    """假 LLM：**记下被调用了几次**，并按给定表逐次返回。

    调用次数是这个文件里最要紧的观测量——"缓存命中了"这句话的全部内容就是
    「`completion` 没有再被调用」。
    """

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.outputs) - 1)
        return self.outputs[index]

    @property
    def calls(self) -> int:
        return len(self.prompts)


STOP = '{"action": "stop", "reason": "探完了"}'


# ── schema 校验 ───────────────────────────────────────────────────────────


class TestParseAction:
    """**T5 的 Verify 原话：「schema 校验拒绝畸形输出」。**

    口径与 `protocol.response_from_wire` 一致：**未知键一律拒绝，不是忽略**
    （见 `decide.py` 的判断 1）。
    """

    def test_probe(self) -> None:
        action = parse_action(
            '{"action": "probe", "prefix": "2001:db8:dead::/64", "count": 8, "reason": "先摸摸底"}'
        )
        assert action.kind == "probe"
        assert action.prefix == PREFIX
        assert action.count == 8
        assert action.reason == "先摸摸底"

    def test_report(self) -> None:
        action = parse_action(
            '{"action": "report", "prefix": "2001:db8:dead::/64", "aliased": true, "reason": "全回了"}'
        )
        assert action.kind == "report"
        assert action.aliased is True

    def test_report_false(self) -> None:
        """`false` 与"键不存在"必须分得开——所以判 `is False`，不是 `falsy`。"""
        action = parse_action(
            '{"action": "report", "prefix": "2001:db8:dead::/64", "aliased": false, "reason": "有空洞"}'
        )
        assert action.aliased is False

    def test_stop(self) -> None:
        assert parse_action(STOP).kind == "stop"

    # ── 拒绝 ──────────────────────────────────────────────────────────

    def test_a_missing_reason_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "stop"}')

    @pytest.mark.parametrize("reason", ['""', '"   "', "null", "42"])
    def test_an_empty_or_non_string_reason_is_refused(self, reason: str) -> None:
        """P1 的「决策理由 | 可读」那一格（`:509` 一带）。**一个可以省掉的
        理由，在第一次赶时间时就会被省掉**——所以它必须是校验的一部分。"""
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "stop", "reason": %s}' % reason)

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(MalformedDecision) as excinfo:
            parse_action('{"action": "stop", "reason": "x", "confidence": 0.9}')
        assert "confidence" in str(excinfo.value)

    def test_a_per_kind_field_on_the_wrong_kind_is_refused(self) -> None:
        """`stop` 不接受 `prefix`。宽松处理（"多余的就忽略"）等于猜。"""
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "stop", "prefix": "2001:db8:dead::/64", "reason": "x"}')

    def test_an_unknown_action_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "explode", "reason": "x"}')

    def test_a_missing_field_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "probe", "prefix": "2001:db8:dead::/64", "reason": "x"}')

    @pytest.mark.parametrize("aliased", ["1", "0", '"true"', "null"])
    def test_aliased_must_be_a_json_boolean(self, aliased: str) -> None:
        """**`1` / `0` 不接受。** 收下 `1` 意味着某一天有人写成 `2` 也会被收，
        而 `aliased_caught` 算的是布尔。"""
        text = (
            '{"action": "report", "prefix": "2001:db8:dead::/64",'
            f' "aliased": {aliased}, "reason": "x"}}'
        )
        with pytest.raises(MalformedDecision) as excinfo:
            parse_action(text)
        assert "true" in str(excinfo.value) or "false" in str(excinfo.value)

    @pytest.mark.parametrize("count", ["0", "-3", '"8"', "1.5", "true", "null"])
    def test_count_must_be_a_positive_integer(self, count: str) -> None:
        text = (
            '{"action": "probe", "prefix": "2001:db8:dead::/64",'
            f' "count": {count}, "reason": "x"}}'
        )
        with pytest.raises(MalformedDecision):
            parse_action(text)

    def test_count_beyond_the_budget_is_not_this_layer_s_business(self) -> None:
        """**预算够不够不在这里判**——那是 `BudgetLedger` 的职责（runner 侧），
        而"一个规则两处实现"正是本仓库反复在防的漂移。这里只管形态。"""
        assert parse_action(
            '{"action": "probe", "prefix": "2001:db8:dead::/64", "count": 1000000000, "reason": "x"}'
        ).count == 10**9

    @pytest.mark.parametrize("prefix", ["not-a-prefix", "2001:db8:dead::1/129", "999.1.1.1/24"])
    def test_an_invalid_prefix_is_refused(self, prefix: str) -> None:
        text = '{"action": "probe", "prefix": "%s", "count": 1, "reason": "x"}' % prefix
        with pytest.raises(MalformedDecision):
            parse_action(text)

    def test_a_non_string_prefix_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "probe", "prefix": 64, "count": 1, "reason": "x"}')

    def test_a_top_level_array_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('[{"action": "stop", "reason": "x"}]')

    def test_a_json_scalar_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('"stop"')

    def test_broken_json_is_refused(self) -> None:
        with pytest.raises(MalformedDecision):
            parse_action('{"action": "stop", "reason": ')

    def test_the_fenced_output_is_refused_rather_than_unwrapped(self) -> None:
        """``` 围栏里的 JSON 也拒绝。

        **这一条是刻意的严格**：渲染 prompt 时已经说了"只输出一个 JSON
        对象，不要别的东西"（`prompts/__init__.py`）。在解析器里悄悄剥围栏，
        等于把这个约定从"要求"降成"建议"——而下一次模型会输出别的花样，
        剥围栏的代码就得跟着长。要放宽，就改 prompt 并让键跟着变。
        """
        with pytest.raises(MalformedDecision):
            parse_action('```json\n{"action": "stop", "reason": "x"}\n```')

    def test_the_raw_text_travels_with_the_error(self) -> None:
        """**畸形输出要能被人看见长什么样。** 否则唯一的处置就是重跑一次
        然后祈祷——而复现问题需要的正是那串原文。"""
        raw = '{"action": "stop"'
        with pytest.raises(MalformedDecision) as excinfo:
            parse_action(raw)
        assert excinfo.value.raw == raw

    def test_the_origin_is_named_in_the_message(self) -> None:
        """命中路径上的畸形与模型刚输出的畸形，处置完全不同，所以消息要区分。"""
        with pytest.raises(MalformedDecision) as excinfo:
            parse_action("{}", origin="缓存中的原文")
        assert "缓存中的原文" in str(excinfo.value)


# ── 工具池 ────────────────────────────────────────────────────────────────


class TestToolPool:
    def test_names_are_sorted(self) -> None:
        pool = ToolPool([Tool("report", "b"), Tool("probe", "a")])
        assert pool.names == ("probe", "report")

    def test_construction_order_does_not_matter(self) -> None:
        """排序规则只在 `ToolPool` 里有一处（`prompts.render` 刻意不再排一次）。"""
        one = ToolPool([Tool("report", "b"), Tool("probe", "a")])
        other = ToolPool([Tool("probe", "a"), Tool("report", "b")])
        assert one.as_pairs() == other.as_pairs()

    def test_duplicate_names_are_refused(self) -> None:
        with pytest.raises(ValueError):
            ToolPool([Tool("probe", "a"), Tool("probe", "b")])

    def test_as_mapping_agrees_with_as_pairs(self) -> None:
        pool = make_pool()
        assert dict(pool.as_pairs()) == dict(pool.as_mapping())

    def test_containment(self) -> None:
        pool = make_pool()
        assert "probe" in pool
        assert "sample" not in pool


# ── 状态：与到达顺序无关 ──────────────────────────────────────────────────


def replies(*addresses: str, hop_limit: int = 64) -> list:
    from src.adapter.types import Response

    return [
        Response(status="received", dst=a, src=a, hop_limit=hop_limit, rtt_ms=1.0)
        for a in addresses
    ]


def silence(*addresses: str) -> list:
    from src.adapter.types import Response

    return [Response(status="sent_no_reply", dst=a) for a in addresses]


class TestStateIsOrderIndependent:
    """**bug 2 的修法在状态侧的落点**（设计方案 `:564`）。

    并发下回包到达顺序每次都不同。如果那个顺序能渗进 `state`，缓存键就会跟着
    变，于是**缓存永不命中——而报错是静默的**（看起来只是"这次没命中"）。
    """

    def test_initial_dedupes_and_sorts(self) -> None:
        state = make_state(prefixes=(OTHER_PREFIX, PREFIX, PREFIX))
        assert state.prefixes == tuple(sorted({PREFIX, OTHER_PREFIX}))

    def test_observing_in_reverse_gives_an_equal_state(self) -> None:
        batch = replies(*[f"2001:db8:dead::{i:x}" for i in range(1, 9)])
        forward = make_state().observe(batch)
        backward = make_state().observe(reversed(batch))
        assert forward == backward

    def test_observing_in_two_batches_gives_an_equal_state(self) -> None:
        """真实并发就是"分几批回来"，而批次边界是运行时决定的。"""
        batch = replies(*[f"2001:db8:dead::{i:x}" for i in range(1, 9)])
        one_shot = make_state().observe(batch)
        in_pieces = make_state().observe(batch[:3]).observe(batch[3:])
        assert one_shot == in_pieces

    def test_observing_in_reverse_gives_the_same_cache_key(self) -> None:
        """**这是那条"缓存永不命中"的直接反证。** 不只是状态相等，
        而是**键相等**——中间还隔着 `canonical_value()` 与摘要。"""
        batch = replies(*[f"2001:db8:dead::{i:x}" for i in range(1, 9)])
        pool = make_pool()

        def key_of(state: Stage1State) -> str:
            return CacheKey.of(
                state=state.canonical_value(),
                seed=1,
                prompt="p",
                tool_pool=pool.as_mapping(),
                config=CONFIG,
            ).hex

        assert key_of(make_state().observe(batch)) == key_of(make_state().observe(reversed(batch)))

    def test_counts_are_aggregated_not_listed(self) -> None:
        state = make_state().observe(replies("2001:db8:dead::1", "2001:db8:dead::2"))
        assert state.probes[PREFIX] == 2
        assert state.replies[PREFIX] == 2
        assert state.failures[PREFIX] == 0

    def test_no_reply_counts_as_a_probe_but_not_a_reply(self) -> None:
        state = make_state().observe(silence("2001:db8:dead::1"))
        assert state.probes[PREFIX] == 1
        assert state.replies[PREFIX] == 0

    def test_send_failed_counts_as_a_probe(self) -> None:
        """三态里 `send_failed` 是一次**真实发生过的尝试**（`:338`）。
        把它从 `probes` 里漏掉，会让「收到响应常常是坏消息」（`:138`）
        这个现象在状态里看不见。"""
        from src.adapter.types import Response

        failed = [Response(status="send_failed", dst="2001:db8:dead::1", error="no CAP_NET_RAW")]
        state = make_state().observe(failed)
        assert state.probes[PREFIX] == 1
        assert state.failures[PREFIX] == 1
        assert state.replies[PREFIX] == 0

    def test_hop_limits_are_a_set(self) -> None:
        state = make_state().observe(replies("2001:db8:dead::1", hop_limit=64)).observe(
            replies("2001:db8:dead::2", hop_limit=63)
        )
        assert state.hop_limits == frozenset({64, 63})

    def test_the_canonical_value_is_always_canonicalizable(self) -> None:
        """**加字段的人必须被这条挡住。** 将来往 `Stage1State` 里加一个
        `list` 或一个时间戳，键会算不出来（`CanonicalError`），而不是
        算出一个随顺序漂移的键。"""
        state = (
            make_state(prefixes=(PREFIX, OTHER_PREFIX))
            .observe(replies("2001:db8:dead::1"))
            .with_report(PREFIX, aliased=True)
            .next_epoch()
        )
        assert canonical(state.canonical_value())

    def test_render_is_deterministic_and_order_free(self) -> None:
        """`render()` 是 prompt 的一部分，而 prompt 的字节进键。"""
        batch = replies(*[f"2001:db8:dead::{i:x}" for i in range(1, 5)])
        assert make_state().observe(batch).render() == make_state().observe(
            reversed(batch)
        ).render()

    def test_render_uses_a_logical_epoch_not_a_clock(self) -> None:
        """`:564` 明写「时间类字段用逻辑序号而非墙钟」。**epoch 就是那个逻辑序号**——
        所以它必须是整数且由调用方推进，而不是 `time.time()`。"""
        state = make_state()
        assert isinstance(state.epoch, int)
        assert state.next_epoch().epoch == state.epoch + 1

    def test_epoch_change_changes_the_key(self) -> None:
        pool = make_pool()

        def key_of(state: Stage1State) -> str:
            return CacheKey.of(
                state=state.canonical_value(),
                seed=1,
                prompt="p",
                tool_pool=pool.as_mapping(),
                config=CONFIG,
            ).hex

        assert key_of(make_state()) != key_of(make_state().next_epoch())


class TestPrefixMatching:
    """最长前缀匹配**自带一份**（`decide.py` 的 `prefix_for`）。

    不 import `src/scoring/` 是一个**硬约束**：那一层必须有真值
    （见 `src/scoring/__init__.py`），而本模块跑在策略进程里，导入它等于
    把真值拖进策略侧命名空间，直接违反 D7。「复用现有函数」在这里是不能做的优化。
    """

    def test_longest_match_wins(self) -> None:
        state = make_state(prefixes=("2001:db8::/32", PREFIX))
        assert state.prefix_for("2001:db8:dead::9") == PREFIX

    def test_longest_match_wins_across_three_levels(self) -> None:
        """三层嵌套，**且要盯住"第一个匹配就返回"这种写法**。

        `initial()` 会把这些前缀按文本排序，而排出来的顺序**不是**由长到短
        （`2001:db8:dead::/64` 排在 `2001:db8:dead:beef::/80` 前面，因为 `/`
        比 `b` 小）。于是"取第一个匹配"会返回最短的那个——这一条把它挡住。
        """
        nets = ("2001:db8::/32", "2001:db8:dead::/64", "2001:db8:dead:beef::/80")
        state = make_state(prefixes=nets)
        assert state.prefix_for("2001:db8:dead:beef::7") == "2001:db8:dead:beef::/80"
        assert state.prefix_for("2001:db8:dead::7") == "2001:db8:dead::/64"
        assert state.prefix_for("2001:db8:cafe::7") == "2001:db8::/32"

    def test_the_shorter_prefix_answers_outside_the_longer_one(self) -> None:
        state = make_state(prefixes=("2001:db8::/32", PREFIX))
        assert state.prefix_for("2001:db8:cafe::9") == "2001:db8::/32"

    def test_an_address_outside_every_prefix_is_refused(self) -> None:
        """静默归到某个前缀下，会让一个界外探测变成一次正常的观测计数。"""
        with pytest.raises(DecisionError):
            make_state().prefix_for("2001:db8:cafe::1")

    def test_an_invalid_address_is_refused(self) -> None:
        with pytest.raises(DecisionError):
            make_state().prefix_for("not-an-address")

    def test_an_invalid_prefix_is_refused_at_construction(self) -> None:
        """拼错的前缀若不在这里拦，会表现成「回包匹不上任何前缀」——更难查。"""
        with pytest.raises(DecisionError):
            make_state(prefixes=("2001:db8:dead::/999",))

    def test_family_mismatch_does_not_match(self) -> None:
        with pytest.raises(DecisionError):
            make_state().prefix_for("192.0.2.1")


class TestStateUpdates:
    def test_report_records_and_overwrites(self) -> None:
        state = make_state().with_report(PREFIX, aliased=True)
        assert state.reported[PREFIX] is True
        assert state.with_report(PREFIX, aliased=False).reported[PREFIX] is False

    def test_reporting_an_unknown_prefix_is_refused(self) -> None:
        with pytest.raises(DecisionError):
            make_state().with_report(OTHER_PREFIX, aliased=True)

    def test_budget_view_is_local(self) -> None:
        """`TestBudgetIsNotLocal` 断言的是"策略改自己这份视图不影响评分"；
        这里只钉住"它能改、而且改的是另一个对象"。"""
        state = make_state(budget=100)
        assert state.with_budget_left(42).remaining_budget == 42
        assert state.remaining_budget == 100


# ── decide()：缓存接线 ────────────────────────────────────────────────────


class TestCacheWiring:
    def test_a_miss_calls_the_model_and_stores(self) -> None:
        cache = DecisionCache()
        completion = CountingCompletion(STOP)
        decision = decide(
            state=make_state(),
            tool_pool=make_pool(),
            seed=1,
            config=CONFIG,
            cache=cache,
            completion=completion,
        )
        assert completion.calls == 1
        assert decision.source == "model"
        assert decision.cache_hit is False
        assert len(cache) == 1

    def test_a_second_identical_call_hits_and_never_calls_the_model(self) -> None:
        """**"缓存命中了"的全部内容就是这一条断言。**"""
        cache = DecisionCache()
        completion = CountingCompletion(STOP)
        for _ in range(3):
            decision = decide(
                state=make_state(),
                tool_pool=make_pool(),
                seed=1,
                config=CONFIG,
                cache=cache,
                completion=completion,
            )
        assert completion.calls == 1
        assert decision.source == "cache"
        assert decision.cache_hit is True

    def test_changing_the_tool_pool_re_asks_the_model(self) -> None:
        """**A1 与 A2 的唯一差别就是工具池**（`:618`），而这个 bug（`:563`）
        正落在 P1 的对照上：工具池变了却不重新问，等于把 A2 当 A1 跑。"""
        cache = DecisionCache()
        completion = CountingCompletion(STOP)
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=completion,
        )
        decide(
            state=make_state(),
            tool_pool=make_pool(Tool("sample", "在一个前缀里抽 n 个地址")),
            seed=1,
            config=CONFIG,
            cache=cache,
            completion=completion,
        )
        assert completion.calls == 2

    def test_editing_the_template_re_asks_the_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**改模板也必须重新问模型。**

        这条是 `prompt` 那个分量**唯一不可替代**的场景：prompt 渲染出来的内容
        全部来自工具池与状态，而这两样各自已经在键里了——所以"prompt 入键"看起来
        是冗余的。**它不冗余的地方正是这里**：`DECIDER_TEMPLATE` 这段文字本身
        不在键里，它只通过渲染后的字节进键。少了这个分量，改一句模板会**静默地
        全量命中旧缓存**，改动被误判为无效——bug 1 的形状（`:563`）。
        """
        import src.agent.prompts as prompts_module

        cache = DecisionCache()
        completion = CountingCompletion(STOP)
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=completion,
        )
        monkeypatch.setattr(
            prompts_module, "DECIDER_TEMPLATE", prompts_module.DECIDER_TEMPLATE + "\n另外：先看左边。\n"
        )
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=completion,
        )
        assert completion.calls == 2

    def test_changing_the_config_re_asks_the_model(self) -> None:
        cache = DecisionCache()
        completion = CountingCompletion(STOP)
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=completion,
        )
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1,
            config={**CONFIG, "temperature": 0.7}, cache=cache, completion=completion,
        )
        assert completion.calls == 2

    def test_the_decision_carries_the_prompt_it_used(self) -> None:
        """报告里要能贴出「它看到了什么」（P1 的「决策理由 | 可读」）。"""
        decision = decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            completion=CountingCompletion(STOP),
        )
        assert PREFIX in decision.prompt
        assert decision.action.kind == "stop"
        assert decision.raw == STOP

    def test_the_prompt_shows_the_observed_counts(self) -> None:
        state = make_state().observe(replies("2001:db8:dead::1", "2001:db8:dead::2"))
        decision = decide(
            state=state, tool_pool=make_pool(), seed=1, config=CONFIG,
            completion=CountingCompletion(STOP),
        )
        assert "已探 2" in decision.prompt


class TestOfflineReplay:
    """`completion=None` = **离线回放模式**：未命中必须炸，不能偷偷联网。

    一次"看起来跑通了"的回放如果悄悄打了 API，它得到的就**不是**原来那次实验
    的结果——它是一份新的、没人记录过的结果，而报告里写着它是复现。
    这与 bug 4（内建 `hash()` 加盐，`:566`）是同一类失效：**静默地换掉了一个
    你以为没变的东西。**
    """

    def test_a_miss_without_a_completion_raises(self) -> None:
        with pytest.raises(DecisionError) as excinfo:
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                cache=DecisionCache(), completion=None,
            )
        assert "离线回放" in str(excinfo.value)

    def test_a_warm_cache_replays_without_a_completion(self) -> None:
        cache = DecisionCache()
        decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=CountingCompletion(STOP),
        )
        replayed = decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=None,
        )
        assert replayed.cache_hit is True
        assert replayed.action.kind == "stop"


class TestMalformedOutputIsCached:
    """**畸形输出也必须进缓存。**（`decide.py` 的 3 处判断之一）

    不缓存它，回放会重新问一次模型——而模型完全可以给出另一个答案，于是"复现"
    复现出了**另一条轨迹**，且没有任何一处会红。

    于是这个类的形状是：**第一次炸（模型输出畸形），第二次也炸，但第二次
    不许再调用 `completion`。** 第二次仍然炸是对的：那份输出本来就是畸形的，
    而"可回放"的定义是**同一条轨迹重复一遍**，不是"第二次变好"。
    """

    def test_a_malformed_first_answer_is_raised_and_recorded(self) -> None:
        cache = DecisionCache()
        completion = CountingCompletion('{"action": "stop"}')  # 缺 reason
        with pytest.raises(MalformedDecision):
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                cache=cache, completion=completion,
            )
        assert completion.calls == 1
        assert len(cache) == 1, "畸形输出没有进缓存 —— 回放会重新问模型，得到另一条轨迹"

    def test_the_replay_raises_the_same_way_without_calling_the_model(self) -> None:
        cache = DecisionCache()
        completion = CountingCompletion('{"action": "stop"}')
        with pytest.raises(MalformedDecision):
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                cache=cache, completion=completion,
            )
        with pytest.raises(MalformedDecision) as excinfo:
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                cache=cache, completion=completion,
            )
        assert completion.calls == 1, "回放又问了模型一次 —— 那次调用没有任何人记录过"
        assert "缓存中的原文" in str(excinfo.value)

    def test_a_stored_answer_is_re_validated_on_every_read(self) -> None:
        """**缓存不能成为绕过校验的后门。**

        做法：先正常跑一次拿到**真实的键**，再把一条非法输出塞进那个键下
        （模拟"被手改过的缓存文件"、或"由更宽松的旧版本写下的条目"）。
        读出来必须仍然被拒——这正是 `cache.DecisionCache` 只存**原文**、
        不存解析结果的理由。
        """
        cache = DecisionCache()
        first = decide(
            state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
            cache=cache, completion=CountingCompletion(STOP),
        )
        cache.store(
            first.key,
            '{"action": "probe", "prefix": "2001:db8:dead::/64", "count": -1, "reason": "x"}',
        )
        with pytest.raises(MalformedDecision):
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                cache=cache, completion=CountingCompletion(STOP),
            )


class TestIdentityIsPinned:
    """LLM 身份六项必须齐全（`:588`），而**缺项要在花钱之前炸**。"""

    @pytest.mark.parametrize("field", LLM_IDENTITY_KEYS)
    def test_a_missing_identity_field_is_refused(self, field: str) -> None:
        incomplete = {k: v for k, v in CONFIG.items() if k != field}
        with pytest.raises(DecisionError) as excinfo:
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=incomplete,
                completion=CountingCompletion(STOP),
            )
        assert field in str(excinfo.value)

    def test_the_check_happens_before_the_model_is_called(self) -> None:
        """**顺序要紧。** 一份没有身份的 config，它的键是合法的、缓存是可加载的、
        结果看起来完全正常——只是没人知道它对应哪个模型。而在发现问题之前
        先把钱花掉，是这条检查最没意义的失效方式。"""
        completion = CountingCompletion(STOP)
        with pytest.raises(DecisionError):
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1,
                config={"model": "claude-sonnet-5-5"}, completion=completion,
            )
        assert completion.calls == 0

    def test_a_completion_returning_a_non_string_is_refused(self) -> None:
        with pytest.raises(DecisionError):
            decide(
                state=make_state(), tool_pool=make_pool(), seed=1, config=CONFIG,
                completion=lambda prompt: {"action": "stop"},  # type: ignore[arg-type,return-value]
            )


# D7 的导入图断言**刻意不在本文件里**：pytest 进程早就 import 过 `oracle`
# 与 `runner`（`test_isolation.py` 的模块级 import），在这里断言 `sys.modules`
# 只会永远失败或永远通过，取决于收集顺序。它必须跑在**干净子进程**里——
# 见 `test_isolation.py::TestAgentImportGraph`。
