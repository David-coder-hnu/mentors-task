"""编排层（`src/experiment/`）的测试。

编排层此前**只存在于一个测试里**：`test_b0.py::TestB0ThroughTheRealRunner`。
这一组把它当**生产代码**测，所以重点不在「B0 算得对不对」（那是 `test_b0.py`
的事），而在**编排层自己那几条会静默失效的地方**：

| 测的是 | 失效了会怎样 |
|---|---|
| 靶场重置在子进程起飞**之前** | 第一轮探测打在上一轮的状态上——症状是「结果飘」 |
| 退出码非零就**炸**、不返回半个结果 | 截断的跑法看起来完全正常，`score()` 照算 |
| 每一趟**新的** executor / 账本 | 上一趟的包落进这一趟的分母 |
| config 整张矩阵只读**一次** | 「所有行用同一份真值」变成一句要读代码才能确认的话 |
| 逐行记录带上 `Score` 的**每一个**字段 | 端点定义改了之后表里静默少一列 |

最后一组跑的是**真的子进程**（`spawn_strategy` → `strategy_child` →
`b0_probe_all:run`）+ **真的 `SurfaceServer`** + **真的 `score()`**。探测出口是
`OfflineExecutor`（排练那一支）：它只判 `aliased`，所以它支撑得起这里每一条
断言——**这里没有一条断言是关于「谁更会绕开 aliased」的**，那种结论只能出自
真靶场（`real.py`），而它的数才有资格进报告。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from src.adapter.probe_pool import ProbeRun, ProbeTarget
from src.adapter.types import Response
from src.experiment import (
    AccumulatingExecutor,
    ExperimentSpec,
    MatrixResult,
    OfflineExecutor,
    RowResult,
    SessionFailed,
    SessionResult,
    load_stage1,
    main,
    run_matrix,
    run_session,
    stride_sampler,
)
from src.experiment.driver import DEFAULT_ROW, SMOKE_TABLE_ROWS, _parse_seeds
from src.scoring.score import Score

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "stage1_config.json"

#: 今天唯一存在的那条策略。其余四条（B1/B4/M1/M2）在 `SMOKE_TABLE_ROWS` 里
#: 占着位置，但**需要一条还不存在的种子通道**才能跑（见 `session` 模块 docstring）。
B0 = "src.baselines.b0_probe_all:run"

#: config 的形状（由 `tests/unit/test_stage1_config.py` 现算一遍再断言）。
CONFIG_PREFIXES = 32
CONFIG_ALIASED = 8
CONFIG_BUDGET = 300


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _aliased_prefixes() -> list[str]:
    truth, _, _ = load_stage1(CONFIG_PATH)
    return [p for p, fields in truth.prefixes.items() if fields["aliased"]]


class _LoggingOffline(OfflineExecutor):
    """排练靶场 + 一句「我跑过了」。用来钉**顺序**，不改变回包判据。

    「靶场重置发生在子进程起飞之前」这件事没有别的观察点：回包内容与它无关，
    而 `reset_target` 只是一个回调。能观察到的只有**事件顺序**——把
    「重置」与「第一次真的发了探测」记进同一条时间线。
    """

    def __init__(
        self,
        aliased_prefixes: Iterable[str],
        prefixes: Sequence[str],
        events: list[str],
    ) -> None:
        super().__init__(aliased_prefixes, prefixes)
        self._events = events

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        self._events.append("probe")
        return super().run(targets)


def _spec(
    events: list[str] | None = None,
    *,
    budget: int | None = None,
    timeout_s: float = 120.0,
) -> ExperimentSpec:
    """一张「离线跑 B0」的 spec。`events` 给了就记时间线。"""
    _, prefixes, _ = load_stage1(CONFIG_PATH)
    aliased = _aliased_prefixes()

    def factory(_seed: int):
        if events is None:
            return OfflineExecutor(aliased, prefixes)
        return _LoggingOffline(aliased, prefixes, events)

    def reset_target(seed: int) -> None:
        if events is not None:
            events.append(f"reset:{seed}")

    return ExperimentSpec(
        config_path=CONFIG_PATH,
        executor_factory=factory,
        reset_target=reset_target,
        cwd=REPO_ROOT,
        timeout_s=timeout_s,
        budget=budget,
    )


def _one_session(seed: int = 0, **kwargs) -> SessionResult:
    """跑一趟 B0，返回那一格。**走的是 `run_matrix`**，不是内部函数。"""
    result = run_matrix(_spec(**kwargs), [B0], [seed])
    assert len(result.rows) == 1
    return result.rows[0].session


# ── 采样器 ──────────────────────────────────────────────────────────────────


class TestStrideSampler:
    """Stage 1 的确定性采样规则。"""

    def test_it_takes_consecutive_addresses_starting_at_the_first_host(self) -> None:
        """**跳过全零主机地址**（anycast），与 `scripts/perf_calibrate` 同一条规避。

        取到 `::0` 会让「这个地址回不回包」里混进一层与策略无关的东西——它是
        子网路由器本身，不是子网里的一个主机。
        """
        first, second = stride_sampler("2001:db8:5::/64", 2)
        assert first == "2001:db8:5::1"
        assert second == "2001:db8:5::2"

    def test_it_is_deterministic(self) -> None:
        """同一个 `(prefix, n)` 两次给同一串——否则整组实验不可回放。"""
        assert stride_sampler("2001:db8:5:1f::/64", 9) == stride_sampler(
            "2001:db8:5:1f::/64", 9
        )

    def test_it_gives_exactly_n_addresses(self) -> None:
        for n in (1, 9, 10):
            assert len(stride_sampler("2001:db8:5::/64", n)) == n

    def test_n_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="n 至少为 1"):
            stride_sampler("2001:db8:5::/64", 0)

    def test_a_prefix_too_small_for_n_is_refused(self) -> None:
        """**不静默截断。** `/126` 只有 4 个地址，要 5 个就是错的。

        静默给 4 个的话，`SurfaceServer` 会在几十行之外报「sampler 给了 4 个
        地址，要的是 5 个」——一个指向现场的错，而原因在这里。
        """
        with pytest.raises(ValueError, match="取不出"):
            stride_sampler("2001:db8:5::/126", 5)


# ── 攒包的那一层 ────────────────────────────────────────────────────────────


class _ScriptedExecutor:
    """按剧本回包的假 executor —— 只用来测 `AccumulatingExecutor` 自己。"""

    def __init__(self, batches: list[tuple[int, int]]) -> None:
        #: `(回包数, 发送失败数)` 一批一条。
        self._batches = list(batches)
        self.calls: list[list[str]] = []

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        dsts = [t.dst if isinstance(t, ProbeTarget) else str(t) for t in targets]
        self.calls.append(dsts)
        n, failed = self._batches.pop(0) if self._batches else (len(dsts), 0)
        responses = tuple(
            Response(status="sent_no_reply", dst=dst) for dst in dsts[:n]
        )
        return ProbeRun(responses=responses, sent=len(dsts), send_failed=failed)


class TestAccumulatingExecutor:
    """`ProbeExecutor.run()` 是逐批的，`score()` 要的是整趟的。这一层补那个落差。"""

    def test_it_returns_the_inner_run_untouched(self) -> None:
        """**透明**：`SurfaceServer` 要的 `responses` / `send_failed` 原样过去。

        它若顺手改写了 `ProbeRun`，症状是 `SurfaceServer` 看到的与 executor
        看到的不一样——而这条边上的错都长得像「策略行为怪」。
        """
        inner = _ScriptedExecutor([(1, 0)])
        wrapped = AccumulatingExecutor(inner)
        target = ProbeTarget("2001:db8:5::1")
        returned = wrapped.run([target])
        assert returned.responses[0].dst == "2001:db8:5::1"
        assert returned.sent == 1

    def test_it_accumulates_across_calls_in_order(self) -> None:
        """B0 是逐前缀一次 `sample`，所以一趟里有 32 次 `run`。

        只留最后一次的话，`probes_charged` 会变成 9/10 而不是 300——
        `aliased_waste_ratio` 的分母当场错掉，而它是个比率。
        """
        wrapped = AccumulatingExecutor(_ScriptedExecutor([(1, 0), (2, 0), (0, 0)]))
        wrapped.run([ProbeTarget("2001:db8:5::1")])
        wrapped.run([ProbeTarget("2001:db8:5::2"), ProbeTarget("2001:db8:5::3")])
        wrapped.run([ProbeTarget("2001:db8:5::4")])
        assert [r.dst for r in wrapped.probes()] == [
            "2001:db8:5::1",
            "2001:db8:5::2",
            "2001:db8:5::3",
        ]

    def test_send_failed_accumulates_across_batches(self) -> None:
        """`send_failed` 非零让**整轮无效**（`RangeLedger.reconcile()`），
        所以它必须按趟汇总，而不是只报最后一批。"""
        wrapped = AccumulatingExecutor(_ScriptedExecutor([(1, 2), (1, 3)]))
        wrapped.run([ProbeTarget("2001:db8:5::1")])
        wrapped.run([ProbeTarget("2001:db8:5::2")])
        assert wrapped.send_failed == 5

    def test_a_snapshot_does_not_move_when_more_batches_arrive(self) -> None:
        """`probes()` 给的是**快照**，不是活视图。

        活视图会让「先折了分、后面又来了一批包」这种顺序问题变得不可见——
        而它是这类薄层最典型的错。
        """
        wrapped = AccumulatingExecutor(_ScriptedExecutor([(1, 0), (1, 0)]))
        wrapped.run([ProbeTarget("2001:db8:5::1")])
        before = wrapped.probes()
        wrapped.run([ProbeTarget("2001:db8:5::2")])
        assert len(before) == 1
        assert len(wrapped.probes()) == 2
        assert len(wrapped) == 2

    def test_a_fresh_wrapper_starts_empty(self) -> None:
        """**每一趟一个新的。** 共用实例会把上一趟的包落进这一趟的分母。"""
        assert len(AccumulatingExecutor(_ScriptedExecutor([]))) == 0


# ── 排练靶场 ────────────────────────────────────────────────────────────────


class TestOfflineExecutor:
    """`OfflineExecutor` 只判 `aliased`。**它的边界是刻意的**，不是省事。"""

    #: 外面那条 `/32` **真的套着**里面那条 `/64`。只有 `/64` 是 aliased。
    #: 嵌套是这里全部断言的支点，所以下面第一条测试专门守着它。
    PREFIXES = ("2001:db8::/32", "2001:db8:5::/64")
    ALIASED = ("2001:db8:5::/64",)
    INSIDE_BOTH = "2001:db8:5::1"
    ONLY_OUTER = "2001:db8:6::1"
    OUTSIDE_ALL = "2001:db9::1"

    def _executor(self, aliased: Sequence[str] = ALIASED) -> OfflineExecutor:
        return OfflineExecutor(aliased, self.PREFIXES)

    def _status(self, executor: OfflineExecutor, dst: str) -> str:
        return executor.run([dst]).responses[0].status

    def test_the_two_prefixes_really_are_nested(self) -> None:
        """**这条是给下面那条测试的保险，而且它是被一次变异检验逼出来的。**

        第一版这里写的是 `2001:db8::/48` 与 `2001:db8:1::/64`——它们**并不嵌套**
        （`/48` 钉死第三组为 `0000`，而 `2001:db8:1::` 的第三组是 `0001`）。
        于是「最长匹配」与「首个命中」给出同一个答案，下面那条测试**测不到任何
        东西，而且照样是绿的**。把 `_matched` 改成首个匹配，全部测试仍然通过。

        这种失效是**静默**的：测试绿、代码坏、没人知道。所以把「这两条前缀真的
        嵌套」本身变成一条断言——将来有人改了 `PREFIXES`，这里先红。
        """
        outer, inner = (ipaddress.ip_network(p, strict=False) for p in self.PREFIXES)
        assert inner.subnet_of(outer), f"{inner} 不在 {outer} 里——两条前缀不嵌套"
        assert outer.prefixlen < inner.prefixlen
        assert ipaddress.ip_address(self.INSIDE_BOTH) in inner
        assert ipaddress.ip_address(self.ONLY_OUTER) in outer
        assert ipaddress.ip_address(self.ONLY_OUTER) not in inner
        assert ipaddress.ip_address(self.OUTSIDE_ALL) not in outer

    def test_the_longest_matching_prefix_decides(self) -> None:
        """**这条测的是归属规则，不是回包规则。**

        同一个地址落在两条前缀里，只有**更长**的那条是 aliased：
        * `2001:db8:5::1` 必须回包——最长的命中是那条 aliased 的 `/64`；
        * `2001:db8:6::1` 不回包——它只落在不 aliased 的 `/32` 上。

        写成「第一个命中的前缀说了算」的话，两条都会按 `/32` 判，第一条变红。
        而这件事会真出问题：`score.py` 的归属用的是最长前缀匹配，两边不一致
        会让「靶场按 `/64` 回的包、评分器按 `/32` 记账」——**两边都自洽**。
        """
        executor = self._executor()
        assert self._status(executor, self.INSIDE_BOTH) == "received"
        assert self._status(executor, self.ONLY_OUTER) == "sent_no_reply"

    def test_a_non_aliased_prefix_never_replies(self) -> None:
        assert self._status(self._executor(), self.INSIDE_BOTH) == "received"
        assert self._status(self._executor(aliased=()), self.INSIDE_BOTH) == "sent_no_reply"

    def test_an_address_outside_every_prefix_gets_no_reply(self) -> None:
        """不在任务范围内的目标：**不回包**，而不是炸。

        `SurfaceServer._require_in_scope` 才是那个该炸的地方；这一层只是靶场。
        """
        assert self._status(self._executor(), self.OUTSIDE_ALL) == "sent_no_reply"

    def test_it_never_fails_a_send(self) -> None:
        """排练靶场里发包不会失败——`send_failed` 非零意味着整轮无效，
        而这条路上没有那种事。它的真值来源是 `RunRecord` 而非这里。"""
        run = self._executor().run([self.INSIDE_BOTH, self.ONLY_OUTER])
        assert run.send_failed == 0
        assert run.sent == 2
        assert len(run.responses) == 2

    def test_it_takes_bare_strings_and_probe_targets_alike(self) -> None:
        """`ProbeExecutor` 的契约收 `ProbeTarget | str`。"""
        run = self._executor().run([self.INSIDE_BOTH, ProbeTarget("2001:db8:5::2")])
        assert [r.status for r in run.responses] == ["received", "received"]


# ── 读 config ───────────────────────────────────────────────────────────────


class TestLoadStage1:
    def test_it_reads_the_truth_the_prefixes_and_the_budget(self) -> None:
        truth, prefixes, budget = load_stage1(CONFIG_PATH)
        assert budget == CONFIG_BUDGET
        assert len(prefixes) == CONFIG_PREFIXES
        assert sum(1 for f in truth.prefixes.values() if f["aliased"]) == CONFIG_ALIASED

    def test_the_prefix_order_is_the_configs_writing_order(self) -> None:
        """顺序是**承重的**：B0 的余数摊平与 `0.25` 的精确等号都建立在
        「aliased 是列表前 1/4」上。顺序一变，那个等号变成近似。"""
        _, prefixes, _ = load_stage1(CONFIG_PATH)
        assert prefixes[0] == "2001:db8:5::/64"
        assert prefixes[CONFIG_ALIASED - 1] == "2001:db8:5:7::/64"
        assert prefixes[CONFIG_ALIASED] == "2001:db8:5:8::/64"

    def test_prefixes_are_normalized(self) -> None:
        """config 里的写法与交给策略的写法要一致——两边不一致时，
        `report()` 的逐字命中会静默退化成「被归并」（`score.py` 的第二种规则）。"""
        _, prefixes, _ = load_stage1(CONFIG_PATH)
        for prefix in prefixes:
            assert str(ipaddress.ip_network(prefix, strict=False)) == prefix

    def test_a_config_without_a_budget_is_refused(self, tmp_path: Path) -> None:
        """**不猜默认值。** 猜出来的预算要么花不完、要么被 runner 拒
        （`SurfaceError`），而两种失败都表现为「跑完了，结果不对」。"""
        path = tmp_path / "no_budget.json"
        path.write_text(
            json.dumps(
                {
                    "subnets": [
                        {
                            "ipv6_prefix": "2001:db8:5::/64",
                            "default_response_rate": 1.0,
                            "EUI_response_rate": 1.0,
                            "lower_response_rate": 1.0,
                            "higher_response_rate": 1.0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(KeyError, match="budget"):
            load_stage1(path)


# ── 一次会话，端到端 ────────────────────────────────────────────────────────


class TestOneSessionEndToEnd:
    """**真的子进程** + 真的 `SurfaceServer` + 真的 `score()`。

    B0 的分数**事先可算**（`0.25` / `0`），所以这里写的是两条**精确**断言。
    算出来的数与跑出来的数相等，同时验证了采样、握手、账本、归属规则与端点
    定义——任何一层偏一点，等号就不成立。
    """

    def test_it_spends_the_whole_budget_and_reaches_every_prefix(self) -> None:
        session = _one_session(seed=0)
        assert session.charged == CONFIG_BUDGET, "权威账本没花完——B0 应该恰好花光"
        assert session.probes_run == CONFIG_BUDGET
        assert session.score.probes_charged == CONFIG_BUDGET

    def test_the_measured_waste_ratio_is_exactly_the_predicted_quarter(self) -> None:
        session = _one_session(seed=0)
        assert session.score.aliased_waste_ratio == pytest.approx(0.25, abs=1e-12)
        assert session.score.configured_prefixes_total == CONFIG_PREFIXES
        assert session.score.aliased_prefixes_total == CONFIG_ALIASED

    def test_the_structural_zero_holds_for_both_columns(self) -> None:
        """B0 的 `aliased_caught` 是**构造性 0**（`:508` 要求表注写明），
        而这个 0 与 `report()` 通道的沉默是同一个事实的两面。"""
        session = _one_session(seed=0)
        assert session.reports == (), "B0 声明了——它没有判定环节，不该有声明"
        assert session.score.aliased_caught == 0
        assert session.score.aliased_false_positives == 0
        assert session.score.aliased_misses == CONFIG_ALIASED

    def test_the_target_is_reset_with_the_seed_before_the_child_starts(self) -> None:
        """**顺序断言。** 先起飞再重置的话，第一轮探测打在上一轮的状态上，
        而症状是「结果飘」——不是报错。

        观察点是**事件时间线**：重置的回调与「第一次真的发了探测」谁先。
        """
        events: list[str] = []
        _one_session(seed=7, events=events)
        assert events[0] == "reset:7", f"第一个事件是 {events[0]!r}，不是重置"
        assert "probe" in events, "一次探测都没发生，这条断言观察不到任何东西"
        assert events.index("reset:7") < events.index("probe")

    def test_two_identical_sessions_give_the_same_score(self) -> None:
        """**可回放**是整组实验的前提：不同 seed 之间的差要是真的差，
        不是同一趟跑两遍也能看到的漂移。"""
        assert _one_session(seed=3).score == _one_session(seed=3).score

    def test_each_row_gets_its_own_executor_and_ledger(self) -> None:
        """**串了就是分母错。** 两趟的 `probes_charged` 各自是 300；
        共用一个 executor 的话第二趟会变成 600（或者被账本拒掉）。"""
        result = run_matrix(_spec(), [B0], [0, 1])
        assert [row.session.charged for row in result.rows] == [300, 300]
        assert [row.session.score.probes_charged for row in result.rows] == [300, 300]

    def test_a_row_carries_the_seed_it_ran_with(self) -> None:
        result = run_matrix(_spec(), [B0], [4, 5])
        assert [row.seed for row in result.rows] == [4, 5]
        assert [row.session.seed for row in result.rows] == [4, 5]
        assert {row.strategy for row in result.rows} == {B0}

    def test_the_row_score_is_the_session_score(self) -> None:
        """`RowResult.score` 是 `SessionResult.score` 的**同一份**，不是另算的。"""
        row = run_matrix(_spec(), [B0], [0]).rows[0]
        assert row.score is row.session.score


# ── 退出码是承重的 ──────────────────────────────────────────────────────────


class TestTheExitCodeIsLoadBearing:
    """非零退出的那一趟**不是一次有效的测量**，所以没有数可以折。

    这一组测的是「它会不会安静地降级成『这次跑得不好』」。
    """

    def test_a_missing_strategy_module_raises_and_is_not_scored(self) -> None:
        with pytest.raises(SessionFailed) as caught:
            run_matrix(_spec(), ["src.baselines.does_not_exist:run"], [0])
        assert caught.value.exit_code == 1
        assert caught.value.entry == "src.baselines.does_not_exist:run"
        assert caught.value.seed == 0
        assert "不折分" in str(caught.value)

    def test_a_strategy_that_cannot_cover_the_range_is_not_scored(self) -> None:
        """B0 在一个盖不住任务范围的预算上会**自己炸**（`plan_counts`）。

        这条同时是**真策略的真失败**：`budget=31 < 32 个前缀`。它证明的是
        「策略侧的异常 → 非零退出码 → 编排层不折分」这条链是通的。
        """
        with pytest.raises(SessionFailed) as caught:
            run_matrix(_spec(budget=31), [B0], [0])
        assert caught.value.exit_code == 1
        assert "盖不住" in caught.value.stderr, (
            "子进程的 stderr 没被带上来——事后只能看到「退出码 1」，"
            "而真正的原因（预算盖不住）在子进程里"
        )

    def test_the_stderr_tail_is_short_enough_to_read(self) -> None:
        """消息里带的是**末 12 行**。整份 stderr 会让一条报错刷满一屏，
        而最后 12 行才是 traceback 真正指向的地方。"""
        with pytest.raises(SessionFailed) as caught:
            run_matrix(_spec(), ["src.baselines.does_not_exist:run"], [0])
        body = str(caught.value).split("子进程 stderr 末 12 行：")[-1]
        assert len([line for line in body.splitlines() if line.strip()]) <= 12

    def test_a_failure_stops_the_matrix_instead_of_returning_what_it_has(self) -> None:
        """**立刻炸，不跑完剩下的。**「跑完再说」会让一张缺格子的表
        看起来像一张完整的表。"""
        with pytest.raises(SessionFailed):
            run_matrix(_spec(), [B0, "src.baselines.does_not_exist:run"], [0])
        # 抛出来了就等于没有 MatrixResult 可读——这是这条断言的全部内容。


# ── 矩阵 ────────────────────────────────────────────────────────────────────


class TestTheMatrix:
    def test_it_covers_every_strategy_times_every_seed(self) -> None:
        result = run_matrix(_spec(), [B0], [0, 1, 2])
        assert len(result.rows) == 3
        assert [row.seed for row in result.rows] == [0, 1, 2]

    def test_the_strategy_is_the_outer_loop(self) -> None:
        """配对检验以 `(config, seed)` 为单位（`:559`），所以同一策略的各 seed
        要**连在一起**：中途炸掉时，已经跑完的那些策略是完整的一块。
        顺序反过来会得到 `[0, 0, 1, 1]`——每个策略各半个。"""
        result = run_matrix(_spec(), [B0], [0, 1])
        assert [row.seed for row in result.rows] == [0, 1]

    def test_the_config_is_read_once_for_the_whole_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """150 趟各读一遍 config 不算贵，但它会让「所有行用同一份真值」
        变成一句**要读代码才能确认的话**。读一次、传下去。"""
        import src.experiment.driver as driver

        calls = 0
        real = driver.load_stage1

        def counting(path: Path):
            nonlocal calls
            calls += 1
            return real(path)

        monkeypatch.setattr(driver, "load_stage1", counting)
        run_matrix(_spec(), [B0], [0, 1, 2])
        assert calls == 1, f"config 被读了 {calls} 遍——每一行都可能读到不同的真值"

    def test_the_matrix_is_timed(self) -> None:
        """**门 1 条件 11 要的就是这个数**：「冒烟表有一次实测总时长，
        且与预算上限对得上」。所以它由编排层测，不由脚本估。"""
        result = run_matrix(_spec(), [B0], [0])
        assert result.elapsed_s > 0.0
        assert all(row.session.elapsed_s > 0.0 for row in result.rows)
        # 整张表 ≥ 任何一格——它不是各格的简单相加（有进程启动这类固定开销
        # 摊在里面），但一定不小于最长的那一格。
        assert result.elapsed_s >= max(row.session.elapsed_s for row in result.rows)

    def test_records_carry_every_score_field(self) -> None:
        """**不挑字段。** 手写一张列清单的话，端点定义改了（T14 要动的正是
        端点）它就会**静默地少一列**——而少的那一列恰恰可能是这次改动要看的那一列。

        所以这里的期望值从 `dataclasses.fields(Score)` **现算**。
        """
        record = run_matrix(_spec(), [B0], [0]).records()[0]
        expected = {field.name for field in dataclasses.fields(Score)}
        assert expected <= set(record), f"记录里少了 {expected - set(record)}"
        assert {"strategy", "seed", "charged", "probes_run", "reports", "elapsed_s"} <= set(
            record
        )
        assert record["strategy"] == B0
        assert record["seed"] == 0
        assert record["charged"] == CONFIG_BUDGET
        assert record["reports"] == []

    def test_records_are_json_serializable(self) -> None:
        """记录的唯一去处是磁盘（`--out`），所以它必须是可序列化的。"""
        records = run_matrix(_spec(), [B0], [0]).records()
        assert json.loads(json.dumps(records, ensure_ascii=False)) == records

    def test_rows_are_grouped_by_strategy(self) -> None:
        """分组是纯数据的搬运，所以用两个合成的 `RowResult` 来测它——
        用真跑的话，今天只有一条策略，分不出「分组对了」与「只有一组」。"""
        session = _one_session(seed=0)
        rows = (
            RowResult("a", 0, session),
            RowResult("b", 0, session),
            RowResult("a", 1, session),
        )
        grouped = MatrixResult(rows=rows, elapsed_s=0.0).by_strategy()
        assert set(grouped) == {"a", "b"}
        assert [row.seed for row in grouped["a"]] == [0, 1]
        assert len(grouped["b"]) == 1

    def test_no_strategies_or_no_seeds_is_refused(self) -> None:
        """空矩阵会产出一个**看起来跑通了**的空表。它不该存在。"""
        with pytest.raises(ValueError, match="没有策略"):
            run_matrix(_spec(), [], [0])
        with pytest.raises(ValueError, match="没有 seed"):
            run_matrix(_spec(), [B0], [])

    def test_the_spec_budget_overrides_the_configs(self) -> None:
        """`budget=None` 才是「从 config 读」，给了就是**覆盖**。

        没有这条覆盖，测「策略盖不住范围」就得改 config 文件本身——而
        config 是承重的实验口径（`test_stage1_config.py` 现算每一条断言）。
        """
        rows = run_matrix(_spec(budget=320), [B0], [0]).rows
        assert rows[0].session.charged == 320
        assert rows[0].session.score.probes_charged == 320

    def test_the_session_and_the_row_agree_about_the_strategy(self) -> None:
        result = run_matrix(_spec(), [B0], [0])
        assert result.rows[0].strategy == B0


# ── 命令行 ──────────────────────────────────────────────────────────────────


class TestTheSeedParser:
    def test_a_range_expands(self) -> None:
        assert _parse_seeds("0-2") == (0, 1, 2)

    def test_a_list_keeps_the_input_order(self) -> None:
        """顺序有意义：它决定矩阵里行的顺序，也就决定了日志读起来的顺序。"""
        assert _parse_seeds("3,1,2") == (3, 1, 2)

    def test_a_single_seed_is_allowed(self) -> None:
        assert _parse_seeds("5") == (5,)

    def test_ranges_and_lists_mix(self) -> None:
        assert _parse_seeds("2-4,9") == (2, 3, 4, 9)

    def test_whitespace_is_ignored(self) -> None:
        assert _parse_seeds(" 0 , 1 ") == (0, 1)

    def test_a_reversed_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="倒的"):
            _parse_seeds("5-2")

    def test_a_duplicate_is_refused(self) -> None:
        """重复的 seed 是**同一个配对单位跑两遍**——它会被当成两个独立的
        观测进检验，而它们不是。"""
        with pytest.raises(ValueError, match="重复"):
            _parse_seeds("0,1,0")

    def test_a_negative_seed_is_refused(self) -> None:
        """负 seed 在 `identifier_for_seed` 那一步才炸的话，报错会指向
        ICMPv6 identifier，而真正的问题是「你要了一个不存在的 seed」。"""
        with pytest.raises(ValueError, match="非负"):
            _parse_seeds("-1")

    def test_a_non_integer_is_refused_with_a_readable_message(self) -> None:
        with pytest.raises(ValueError, match="不是整数"):
            _parse_seeds("abc")

    def test_an_empty_spec_is_refused(self) -> None:
        with pytest.raises(ValueError, match="没解析出"):
            _parse_seeds(" , ")


class TestTheCommandLine:
    def test_the_default_row_is_b0_and_the_table_has_five_rows(self) -> None:
        """冒烟表是五行（`:628`）：B0 退化警报、B1 下界、B4 无记忆控制组、
        M1/M2 记忆族两行。**A1/A2 不在 Stage 1 参赛**。

        默认值取 B0 而不是整张表，是因为另外四条今天**跑不起来**
        （缺种子通道）——「一条命令跑完」在那儿会红，**红是对的，但不该是默认**。
        """
        assert len(SMOKE_TABLE_ROWS) == 5
        assert DEFAULT_ROW == SMOKE_TABLE_ROWS[0]
        assert DEFAULT_ROW == B0

    def test_it_runs_a_matrix_and_writes_ascii_only_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """**stdout 必须是 ASCII**——中文 Windows 的控制台是 GBK，一行非
        GBK 字符的 `print` 会让整条命令以 `UnicodeEncodeError` 结束，而那时
        实验其实已经跑完了。中文说明走 stderr（GBK 编得出来）。
        """
        out = tmp_path / "rows.json"
        assert main(["--offline", "--seeds", "0-1", "--out", str(out)]) == 0

        captured = capsys.readouterr()
        captured.out.encode("ascii")  # 非 ASCII 就抛 UnicodeEncodeError
        assert "wall_clock_s" in captured.out

        document = json.loads(out.read_text(encoding="utf-8"))
        assert document["seeds"] == [0, 1]
        assert document["strategies"] == [DEFAULT_ROW]
        assert len(document["rows"]) == 2
        assert document["rows"][0]["aliased_waste_ratio"] == pytest.approx(0.25, abs=1e-12)
        assert document["wall_clock_s"] > 0.0

    def test_it_accepts_repeated_strategy_flags(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "two.json"
        assert main(
            ["--offline", "--seeds", "0", "--strategy", B0, "--strategy", B0, "--out", str(out)]
        ) == 0
        capsys.readouterr()
        document = json.loads(out.read_text(encoding="utf-8"))
        assert document["strategies"] == [B0, B0]
        # 外层是策略、内层是 seed —— 两条同名策略各跑了一遍。
        assert [row["seed"] for row in document["rows"]] == [0, 0]

    def test_no_out_flag_means_nothing_is_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """不写文件也能跑——排练时看一行就够。"""
        assert main(["--offline", "--seeds", "0"]) == 0
        assert "rows=1" in capsys.readouterr().out
        assert list(tmp_path.iterdir()) == []

    def test_a_bad_seed_spec_exits_before_any_child_starts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """参数错了要在**起子进程之前**炸。

        断言的形式是「`run_matrix` 根本没被调到」：单独一条 `pytest.raises`
        在「先跑一趟再炸」的实现上也会绿，而那种实现的代价是几十秒的子进程
        启动——**报错的位置与原因隔着一整趟运行**。
        """
        import src.experiment.driver as driver

        def must_not_run(*args: object, **kwargs: object) -> None:
            raise AssertionError("seed 还没解析成功就已经开始跑了")

        monkeypatch.setattr(driver, "run_matrix", must_not_run)
        with pytest.raises(ValueError, match="重复"):
            main(["--offline", "--seeds", "0,0"])
        capsys.readouterr()
