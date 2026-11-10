"""`score()` 的折分语义 —— 纯 Python，不碰 Docker，**进 hosted CI**。

这组测试盯的是**评分口径**，而评分口径的失效方式和 T3 那组一样：表照出、
数看起来正常，只是判胜负的那个数从一开始就错了。所以这里测的是**边界**——
两个极端、归属规则、以及几条「换个读法数字就变」的地方（`send_failed` 算不算、
重复声明取哪一条、声明与真值粒度错位怎么记）。

真值一律经 T3 的 `stage1_truth_from_config()` 造，不手搓 `Stage1Truth`：
**评分器读的是 T3 的产物**，中间隔一层手搓的真值就测不出两边的口径是否一致。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.adapter.ground_truth import stage1_truth_from_config
from src.adapter.types import Response
from src.scoring.score import RunRecord, Score, ScoringError, score

# ── 造数据 ──────────────────────────────────────────────────────────────────

_ALWAYS = {
    "default_response_rate": 1.0,
    "EUI_response_rate": 1.0,
    "lower_response_rate": 1.0,
    "higher_response_rate": 1.0,
}
_SILENT = {
    "default_response_rate": 0.0,
    "EUI_response_rate": 0.0,
    "lower_response_rate": 0.0,
    "higher_response_rate": 0.0,
}


def _config(*subnets: dict) -> dict:
    return {"subnets": list(subnets)}


def _aliased(prefix: str) -> dict:
    return {"ipv6_prefix": prefix, **_ALWAYS}


def _silent(prefix: str) -> dict:
    return {"ipv6_prefix": prefix, **_SILENT}


def _reply(addr: str) -> Response:
    """收到回包。Stage 1 下 Punching Bag 伪造 `src = dst`，所以 `indirect` 恒为 False。"""
    return Response(status="received", dst=addr, src=addr, icmp_type=129, icmp_code=0)


def _quiet(addr: str) -> Response:
    return Response(status="sent_no_reply", dst=addr)


def _failed(addr: str) -> Response:
    return Response(status="send_failed", dst=addr, error="no CAP_NET_RAW")


def _declaring(*reports: tuple[str, bool], probe: str = "2001:db8:cccc::1") -> RunRecord:
    """一条**以声明为主**的运行记录。

    `score()` 拒答零探测的运行（见 `score.py` 的模块 docstring），所以哪怕这组
    用例测的只是声明语义，也得带上一次探测。默认探 `cccc`——它是**沉默的非
    aliased** 前缀，对 `aliased_waste_ratio` 与 `discovered_real_prefixes` 都无
    影响，于是这些用例测的仍然只有声明那一侧。
    """
    return RunRecord(probes=(_quiet(probe),), reports=reports)


def _alias_evidence(prefix: str, n: int = 2) -> tuple[Response, ...]:
    """给一个声明补足最小证据：至少 `n` 次探测、`n` 次回包。"""

    network = prefix.split("/")[0]
    return tuple(_reply(f"{network}{i + 1:x}") for i in range(n))


#: 四个前缀，四类各一 —— 与 T3 的 `SMALL_CONFIG` 同一形状，便于两边对照。
#: aaaa = aliased（唯一一个）；bbbb = 有内容且**有一档是 1.0**（不是 aliased）；
#: cccc = 沉默；dddd = 四档字段全缺（靶场缺省 0，也是沉默）。
MIXED = _config(
    _aliased("2001:db8:aaaa::/64"),
    {"ipv6_prefix": "2001:db8:bbbb::/64", "default_response_rate": 0.05, "EUI_response_rate": 1.0},
    _silent("2001:db8:cccc::/64"),
    {"ipv6_prefix": "2001:db8:dddd::/64"},
)


def _mixed_truth():
    return stage1_truth_from_config(MIXED)


# ── 两个极端（T4 的 Verify 原话）────────────────────────────────────────────


class TestExtremes:
    """Verify：「全 aliased / 全非 aliased」。**极端用例不是「全是真路由器」**
    ——Stage 1 没有路由器（`:692`）。"""

    def test_all_aliased_means_every_probe_is_wasted(self) -> None:
        truth = stage1_truth_from_config(
            _config(_aliased("2001:db8:1::/64"), _aliased("2001:db8:2::/64"))
        )
        run = RunRecord(
            probes=tuple(_reply(f"2001:db8:1::{i:x}") for i in range(3))
            + tuple(_reply(f"2001:db8:2::{i:x}") for i in range(3)),
            reports=(("2001:db8:1::/64", True), ("2001:db8:2::/64", True)),
        )
        result = score(run, truth)

        assert result.aliased_waste_ratio == 1.0  # 全部预算都花在 aliased 上
        assert result.aliased_caught == 2
        assert result.aliased_misses == 0
        assert result.aliased_false_positives == 0

    def test_all_non_aliased_means_zero_waste(self) -> None:
        truth = stage1_truth_from_config(
            _config(
                {"ipv6_prefix": "2001:db8:1::/64", "default_response_rate": 0.5},
                _silent("2001:db8:2::/64"),
            )
        )
        run = RunRecord(
            probes=(_reply("2001:db8:1::1"), _quiet("2001:db8:2::1")),
            reports=(("2001:db8:1::/64", False),),
        )
        result = score(run, truth)

        assert result.aliased_waste_ratio == 0.0
        assert result.aliased_caught == 0
        assert result.aliased_prefixes_total == 0  # 一个 aliased 都没有
        assert result.aliased_misses == 0

    def test_the_extremes_are_the_two_ends_not_two_examples(self) -> None:
        """把两个极端拼在一起，比值必须落回中间——否则上面两条只是巧合。"""
        truth = stage1_truth_from_config(
            _config(_aliased("2001:db8:1::/64"), _silent("2001:db8:2::/64"))
        )
        run = RunRecord(probes=(_quiet("2001:db8:1::1"), _quiet("2001:db8:2::1")))
        assert score(run, truth).aliased_waste_ratio == 0.5


# ── 预算口径 ────────────────────────────────────────────────────────────────


class TestBudgetDenominator:
    def test_ratio_is_over_charged_probes_not_over_replies(self) -> None:
        truth = _mixed_truth()
        run = RunRecord(probes=(_quiet("2001:db8:aaaa::1"), _reply("2001:db8:bbbb::1")))
        result = score(run, truth)
        assert result.aliased_waste_ratio == 0.5
        assert result.probes_charged == 2

    def test_send_failed_is_charged_and_counts_as_waste_on_an_aliased_prefix(self) -> None:
        """**这一条是刻意的口径，不是顺手写出来的。**

        `BudgetLedger.charge()` 先扣后跑，所以发包失败的钱已经花了。剔掉它
        等于让「根本没发出去」比「发出去了没回来」更便宜，而这是个比率端点。
        """
        truth = _mixed_truth()
        run = RunRecord(probes=(_failed("2001:db8:aaaa::1"),))
        result = score(run, truth)

        assert result.probes_charged == 1
        assert result.aliased_waste_ratio == 1.0
        assert result.send_failed == 1  # 同时留一笔：这一轮无效，该重跑

    def test_unattributed_probes_are_in_the_denominator_but_not_the_numerator(self) -> None:
        """任务范围比 config 宽时，探到 config 之外是可能的。

        那笔预算**没有**花在 aliased 上（不进分子），但**确实花了**（进分母）——
        把它从两边一起删掉，就等于让「探错地方」免费。
        """
        truth = _mixed_truth()
        run = RunRecord(
            probes=(_quiet("2001:db8:aaaa::1"), _quiet("2001:db8:ffff::1")),
        )
        result = score(run, truth)

        assert result.probes_unattributed == 1
        assert result.probes_charged == 2
        assert result.aliased_waste_ratio == 0.5

    def test_an_empty_run_is_not_a_zero_ratio(self) -> None:
        """0/0 不是 0，是「没测」。写成 0 会让没跑起来的运行看起来完美。"""
        with pytest.raises(ScoringError, match="一次探测都没有"):
            score(RunRecord(), _mixed_truth())


# ── 归属规则 ────────────────────────────────────────────────────────────────


class TestAttribution:
    def test_the_longest_matching_prefix_wins(self) -> None:
        """与靶场的 trie 同一条规则（`IPv6-trie.hpp:285-311`）。

        `/48` 是 aliased 陷阱，但它下面挂着一个沉默的 `/64`：探 `/64` 里的地址
        不该被记成浪费——**靶场也不会对它回包**。
        """
        truth = stage1_truth_from_config(
            _config(_aliased("2001:db8:1::/48"), _silent("2001:db8:1:2::/64"))
        )
        run = RunRecord(
            probes=(_quiet("2001:db8:1:2::5"), _quiet("2001:db8:1:3::5")),
        )
        result = score(run, truth)

        assert result.probes_unattributed == 0  # 两条都归属到了某个前缀
        assert result.aliased_waste_ratio == 0.5  # 只有走 /48 的那一条算浪费

    def test_a_malformed_target_raises_instead_of_being_counted_unattributed(self) -> None:
        """畸形地址是上游 bug，不是「探到了范围外」。混进 `probes_unattributed`
        会让一个真 bug 长得像一次越界探测。"""
        run = RunRecord(probes=(_quiet("not-an-address"),))
        with pytest.raises(ScoringError, match="不是合法地址"):
            score(run, _mixed_truth())


# ── `aliased_caught` 与声明通道 ─────────────────────────────────────────────


class TestAliasedCaught:
    def test_no_reports_means_zero_caught_however_much_was_probed(self) -> None:
        """**结构性 0**（`:498`）：B0/B1 从不声明，所以这一列按构造恒为 0。

        这不是「表现差」，是「这个端点对它们不可测」——评分器**不得**从行为里
        推断它们认出了什么。这一条就是那个禁令的可执行形式。
        """
        truth = _mixed_truth()
        run = RunRecord(probes=tuple(_reply(f"2001:db8:aaaa::{i:x}") for i in range(5)))
        result = score(run, truth)

        assert result.aliased_caught == 0
        assert result.aliased_misses == 1
        assert result.discovered_real_prefixes == 0  # aaaa 是 aliased，不算「真有内容」

    def test_only_correct_declarations_are_caught(self) -> None:
        truth = _mixed_truth()
        run = RunRecord(
            probes=_alias_evidence("2001:db8:aaaa::/64")
            + _alias_evidence("2001:db8:bbbb::/64"),
            reports=(("2001:db8:aaaa::/64", True), ("2001:db8:bbbb::/64", True)),
        )
        result = score(run, truth)

        assert result.aliased_caught == 1  # 只有 aaaa 报对了
        assert result.aliased_false_positives == 1  # bbbb 是误报
        assert result.aliased_misses == 0

    def test_the_same_prefix_reported_twice_is_counted_once(self) -> None:
        truth = _mixed_truth()
        run = RunRecord(
            probes=_alias_evidence("2001:db8:aaaa::/64"),
            reports=(("2001:db8:aaaa::/64", True), ("2001:db8:aaaa::/64", True)),
        )
        result = score(run, truth)

        assert result.aliased_caught == 1
        assert result.report_flip_flops == 0  # 同向重复不是翻供

    def test_a_retracted_declaration_does_not_count(self) -> None:
        """「正确识别」读作**运行结束时的最终判断**，所以后者胜。

        先报 True 后报 False 是「先猜后修正」——把它算成抓住了，等于奖励
        一次后来被自己否掉的猜测。
        """
        truth = _mixed_truth()
        run = _declaring(("2001:db8:aaaa::/64", True), ("2001:db8:aaaa::/64", False))
        result = score(run, truth)

        assert result.aliased_caught == 0
        assert result.aliased_misses == 1
        assert result.report_flip_flops == 1  # 翻供留痕，但不炸

    def test_a_declaration_finer_than_the_truth_prefix_is_attributed_not_dropped(self) -> None:
        """一个判断，理由写在模块 docstring 里：**选错了会误伤 B5**。

        6SEVEN 的在线判定按六种长度各探 3 次，照它实现的 B5 很可能照自己测的
        那个长度去声明。若要求逐字命中 truth 的键，B5 会在这一列上拿 0——
        而那是我们自己的记账口径造成的。
        """
        truth = _mixed_truth()
        run = RunRecord(
            probes=_alias_evidence("2001:db8:aaaa::/64"),
            reports=(("2001:db8:aaaa::/80", True),),
        )
        result = score(run, truth)

        assert result.aliased_caught == 1
        assert result.reports_refined == 1  # 但不是逐字命中，留痕
        assert result.reports_off_target == 0

    def test_a_declaration_containing_the_truth_prefix_is_off_target(self) -> None:
        """反方向不行：`/48` 的声明**不等于**它下面那个 `/64` 的判定。

        `2001:db8:aaaa::/64` 是 aliased，但报一个包住它的 `2001:db8:aaaa::/48`
        （网络地址 `2001:db8:aaaa::`，落在 /64 里）会被归属；而报 `/48`
        **以上**的粗粒度前缀，其网络地址落在 truth 之外——记 off-target。
        """
        truth = _mixed_truth()
        run = RunRecord(
            probes=_alias_evidence("2001:db8:aaaa::/64"),
            reports=(("2001:db8:aaaa::/48", True), ("2001:db8:ffff::/64", True)),
        )
        result = score(run, truth)

        # 2001:db8:aaaa::/48 的网络地址是 2001:db8:aaaa::，**在** /64 里 → 归属
        assert result.aliased_caught == 1
        assert result.reports_refined == 1
        # 2001:db8:ffff::/64 整个在 config 之外 → 未命中
        assert result.reports_off_target == 1

    def test_an_unparseable_declaration_is_off_target_not_an_exception(self) -> None:
        """策略报了一个连解析都失败的串：那是策略行为，不该让整次评分炸掉。"""
        run = _declaring(("这不是前缀", True))
        result = score(run, _mixed_truth())
        assert result.reports_off_target == 1
        assert result.aliased_caught == 0


# ── 防刷分门槛 ──────────────────────────────────────────────────────────────


class TestTheEndpointResistsGaming:
    """T14：声明必须有证据，且精度不足的 run 不能进入胜负判据。"""

    def test_declaring_everything_aliased_no_longer_maximizes_caught(self) -> None:
        truth = _mixed_truth()
        every_prefix = tuple((p, True) for p in truth.prefixes)
        run = RunRecord(
            probes=(_reply("2001:db8:bbbb::1"),),  # 探一个非 aliased 的，比值最优
            reports=every_prefix,
        )
        result = score(run, truth)

        assert result.aliased_caught == 0
        assert result.aliased_unsubstantiated_claims == 1
        assert result.aliased_false_positives == 3
        assert result.aliased_claim_precision == 0.0
        assert result.aliased_endpoint_admissible is False

    def test_the_gaming_run_and_the_honest_run_are_now_separable(self) -> None:
        truth = _mixed_truth()
        honest = RunRecord(
            probes=_alias_evidence("2001:db8:aaaa::/64"),
            reports=(("2001:db8:aaaa::/64", True),),
        )
        spammer = RunRecord(
            probes=_alias_evidence("2001:db8:bbbb::/64"),
            reports=tuple((p, True) for p in truth.prefixes),
        )

        honest_score = score(honest, truth)
        spammer_score = score(spammer, truth)

        assert honest_score.aliased_caught == 1
        assert spammer_score.aliased_caught == 0
        assert honest_score.aliased_false_positives == 0
        assert spammer_score.aliased_false_positives == 3
        assert honest_score.aliased_endpoint_admissible is True
        assert spammer_score.aliased_endpoint_admissible is False

    def test_one_reply_is_not_enough_evidence(self) -> None:
        truth = _mixed_truth()
        run = RunRecord(
            probes=(_reply("2001:db8:aaaa::1"),),
            reports=(("2001:db8:aaaa::/64", True),),
        )
        result = score(run, truth)
        assert result.aliased_caught == 0
        assert result.aliased_unsubstantiated_claims == 1
        assert result.aliased_endpoint_admissible is False


# ── 次要诊断 ────────────────────────────────────────────────────────────────


class TestDiscoveredRealPrefixes:
    def test_only_replied_non_aliased_prefixes_count(self) -> None:
        truth = _mixed_truth()
        run = RunRecord(
            probes=(
                _reply("2001:db8:aaaa::1"),  # aliased 前缀回了包 —— 但那是垃圾，不算发现
                _reply("2001:db8:bbbb::1"),  # 真有内容 ✓
                _quiet("2001:db8:cccc::1"),  # 没回 ≠ 有内容
            )
        )
        assert score(run, truth).discovered_real_prefixes == 1

    def test_dedup_is_by_prefix_not_by_reply(self) -> None:
        """定义是「前缀数（去重）」（`:482`）——同一前缀回三次仍然只算一个。"""
        truth = _mixed_truth()
        run = RunRecord(
            probes=tuple(_reply(f"2001:db8:bbbb::{i:x}") for i in range(3)),
        )
        assert score(run, truth).discovered_real_prefixes == 1


# ── 输入校验 ────────────────────────────────────────────────────────────────


class TestScoreInvariants:
    """`Score.__post_init__` 的恒等式 —— 一个算错的 `Score` **构造不出来**。"""

    def test_the_caught_and_missed_halves_must_tile_the_aliased_set(self) -> None:
        good = score(RunRecord(probes=(_quiet("2001:db8:aaaa::1"),)), _mixed_truth())
        with pytest.raises(ScoringError, match="恰好铺满"):
            replace(good, aliased_caught=good.aliased_caught + 1)
        with pytest.raises(ScoringError, match="恰好铺满"):
            replace(good, aliased_misses=good.aliased_misses - 1)

    def test_the_ratio_must_stay_a_ratio(self) -> None:
        good = score(RunRecord(probes=(_quiet("2001:db8:aaaa::1"),)), _mixed_truth())
        for bad in (-0.01, 1.01):
            with pytest.raises(ScoringError, match="不在"):
                replace(good, aliased_waste_ratio=bad)

    def test_the_diagnostics_must_stay_inside_the_denominator(self) -> None:
        good = score(RunRecord(probes=(_quiet("2001:db8:aaaa::1"),)), _mixed_truth())
        with pytest.raises(ScoringError, match="不在"):
            replace(good, probes_unattributed=2)
        with pytest.raises(ScoringError, match="不在"):
            replace(good, send_failed=2)


class TestTruthValidation:
    def test_a_truth_prefix_without_the_aliased_key_raises(self) -> None:
        """静默当成「不是 aliased」，会让一个真值导出 bug 表现为一张正常的表。"""
        from src.adapter.truth import Stage1Truth

        truth = Stage1Truth(prefixes={"2001:db8:aaaa::/64": {}})
        with pytest.raises(ScoringError, match="缺少"):
            score(RunRecord(probes=(_quiet("2001:db8:aaaa::1"),)), truth)

    def test_two_keys_on_the_same_network_raise(self) -> None:
        """与靶场读 config 的「重复前缀后者胜」是同一类静默歧义——**这里选择炸**。

        T3 的 `read_subnets()` 按规范化字符串做键，所以它的产物不会有这个形状；
        手搓的真值可能有。
        """
        from src.adapter.truth import Stage1Truth

        truth = Stage1Truth(
            prefixes={
                "2001:db8::/64": {"aliased": True},
                "2001:db8:0:0::/64": {"aliased": False},
            }
        )
        with pytest.raises(ScoringError, match="同一个网络"):
            score(RunRecord(probes=(_quiet("2001:db8::1"),)), truth)


class TestRunRecordValidation:
    def test_lists_are_copied_into_tuples(self) -> None:
        """frozen dataclass 里存一个可变对象，等于留了一条「构造完还能改输入」的路。"""
        run = RunRecord(probes=[_quiet("2001:db8:aaaa::1")], reports=[("2001:db8:aaaa::/64", True)])
        assert isinstance(run.probes, tuple)
        assert isinstance(run.reports, tuple)
        assert isinstance(run.reports[0], tuple)

    def test_non_bool_aliased_raises(self) -> None:
        """`0`/`1` 是 `bool` 的子类之外的近邻——它们在这个端点上的语义没定义。"""
        with pytest.raises(ScoringError, match="期望 bool"):
            RunRecord(reports=[("2001:db8:aaaa::/64", 1)])  # type: ignore[arg-type]

    def test_a_non_response_probe_raises(self) -> None:
        with pytest.raises(ScoringError, match="期望 Response"):
            RunRecord(probes=["2001:db8:aaaa::1"])  # type: ignore[list-item]
