"""`B0`「全探一遍」—— 平凡基线，同时是**退化警报**（T13 / D10）。

这组测试只做一件事：**把 B0 的分配钉到可复现，并证明警报真的有牙。**

警报如果只是「跑一遍看看」就没有牙——它要能在一个**被改坏的 config** 上
真的叫起来。所以 `TestTheAlarmHasTeeth` 那几条不是测 B0 的行为，是测**阈值
本身有分辨力**：把 aliased 占比从 1/4 压到 1/32，同一个函数必须给出相反的判决。

最后一组是 T13 的 Verify 本体：「**B0 单独跑一遍**」——跑的是**真的子进程**
（`spawn_strategy` → `strategy_child` → `b0_probe_all:run`），对着**真的 config**，
再由**真的 `score()`** 折分。它把 T13 → T2 → T3 → T4 串起来，产出那个警报
真正关心的数。
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest

from src.adapter.ground_truth import read_subnets, stage1_truth_from_config
from src.adapter.oracle import Oracle
from src.adapter.probe_pool import ProbeRun
from src.adapter.runner import BudgetLedger, SurfaceServer, spawn_strategy
from src.adapter.types import Response
from src.baselines.b0_probe_all import DEGENERATE_WASTE_FLOOR, plan_counts, run
from src.scoring.score import RunRecord, score

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "stage1_config.json"


class FakeSurface:
    """最小的假 surface —— 只记录 `sample` 被怎么调用的。

    **它是结构性的，不是 `ProbeSurface` 的子类**：`b0_probe_all` 只要求
    「有 `prefixes`、有 `budget_left`、有 `sample`」。用真 `ProbeSurface` 会
    把这一组测试变成协议测试，而这里要测的是**分配**。
    """

    def __init__(self, prefixes: list[str], budget: int | None) -> None:
        self.prefixes = tuple(prefixes)
        self._budget = budget
        #: 按序的 `(prefix, n)`。
        self.calls: list[tuple[str, int]] = []

    @property
    def budget_left(self) -> int | None:
        return self._budget

    def sample(self, prefix: str, n: int) -> list[object]:
        self.calls.append((prefix, n))
        return [object()] * n


def _prefixes(n: int) -> list[str]:
    return [f"2001:db8:5:{i:x}::/64" for i in range(n)]


class TestPlanCounts:
    """预算怎么摊到前缀上。"""

    def test_the_total_is_exactly_the_budget(self) -> None:
        """**不许花不完，也不许超。** 花不完会让 B0 在一列上少算分母，
        超了会被 runner 拒绝（`SurfaceError`）。"""
        for n, budget in [(32, 300), (32, 320), (5, 5), (7, 100), (1, 1)]:
            counts = plan_counts(tuple(_prefixes(n)), budget)
            assert sum(counts) == budget, f"{n} 个前缀 / 预算 {budget}"
            assert len(counts) == n

    def test_counts_differ_by_at_most_one(self) -> None:
        """均摊的定义：每个前缀拿 `⌊budget/n⌋` 或 `⌈budget/n⌉`。"""
        counts = plan_counts(tuple(_prefixes(32)), 300)
        assert max(counts) - min(counts) <= 1
        assert set(counts) <= {9, 10}

    def test_the_remainder_is_not_front_loaded(self) -> None:
        """**这条测的是「别偷懒」。**

        最省事的写法是「前 `budget % n` 个各多探一次」，而它会让多出来的探测
        落在谁头上取决于 **config 里 `subnets` 的书写顺序**——把 aliased 的八行
        从开头挪到中间，B0 测出的 `aliased_waste_ratio` 就变了，而实验里没有
        任何一个自变量是 config 的行序。

        300 // 32 = 9 余 12。偷懒写法给 `counts[0] == 10`；均摊写法给 9。
        """
        counts = plan_counts(tuple(_prefixes(32)), 300)
        assert counts[0] == 9, (
            f"第一个前缀拿了 {counts[0]} 次——余数被前置了。"
            "均摊的写法下，第一个前缀不该自动多拿一次"
        )
        # 12 个前缀各多一次，位置分散：前 3 个里只有下标 2 拿到了。
        assert [i for i, c in enumerate(counts[:6]) if c == 10] == [2, 5]

    def test_a_budget_that_cannot_cover_the_range_is_refused(self) -> None:
        """盖不住就炸，**不静默只盖前几个**。

        静默削掉覆盖范围恰恰会**掩盖**退化——而 B0 的全部用处就是发现退化。
        """
        with pytest.raises(ValueError, match="盖不住"):
            plan_counts(tuple(_prefixes(32)), 31)
        with pytest.raises(ValueError, match="没有前缀"):
            plan_counts((), 300)


class TestRun:
    """`run(surface)` 的行为。"""

    def test_every_prefix_is_visited_once_in_order_and_the_budget_is_spent(self) -> None:
        surface = FakeSurface(_prefixes(32), budget=300)
        run(surface)

        assert [p for p, _ in surface.calls] == _prefixes(32), "前缀没被逐一访问，或顺序不稳"
        assert sum(n for _, n in surface.calls) == 300

    def test_it_never_declares_anything(self) -> None:
        """B0 的 `aliased_caught` 是**构造性 0**，不是「表现差」（`:508`）。

        这条断言的形式是「**根本没有这条路**」：假 surface 上**没有** `report`
        属性，所以 B0 一旦试图声明就会 `AttributeError`，测试变红。这比
        「数一数 `report` 被调了几次」强——后者在 B0 自己加一个 `report` 的
        私有封装之后就测不到了。
        """
        surface = FakeSurface(_prefixes(32), budget=300)
        assert not hasattr(surface, "report"), "假 surface 不该有 report，否则这条测试失去意义"
        run(surface)  # 不抛 = B0 没有试图声明

    def test_an_unknown_budget_is_an_error_not_a_guess(self) -> None:
        """`budget_left is None` 时必须炸——**不猜默认值**。

        猜小了花不完预算，猜大了被 runner 拒绝（`SurfaceError`），而两种失败的
        现场都是「策略跑完了，结果不对」。这正是 T13 撞到的那个接口缺口
        （见 `tests/unit/test_isolation.py::TestHandshakeCarriesTheBudget`）。
        """
        surface = FakeSurface(_prefixes(32), budget=None)
        with pytest.raises(ValueError, match="budget_left 是 None"):
            run(surface)
        assert surface.calls == [], "炸之前不该已经发了探测"


class TestTheAlarmHasTeeth:
    """**退化警报的阈值必须有分辨力**——否则它只是一段散文。

    警报的判据是 `aliased_waste_ratio` 的可用跨度够不够大（见
    `b0_probe_all` 模块 docstring 第二节）。B0 均分给每个前缀，于是它的
    `aliased_waste_ratio` **恰等于 aliased 前缀的占比**——这个等式是下面
    所有断言的支点，也是 `test_stage1_config.py` 里那条精确断言的依据。
    """

    def _b0_waste(self, n_prefixes: int, n_aliased: int, budget: int = 300) -> float:
        """按 B0 的分配算它花在 aliased 前缀上的探测占比。"""
        counts = plan_counts(tuple(_prefixes(n_prefixes)), budget)
        wasted = sum(counts[:n_aliased])  # aliased 排在前面，同 config
        return wasted / budget

    def test_a_degenerate_config_fires_the_alarm(self) -> None:
        """1/32 的 aliased 占比 ⇒ 绕开 aliased 没多少可省 ⇒ 报警。"""
        waste = self._b0_waste(n_prefixes=32, n_aliased=1)
        assert waste < DEGENERATE_WASTE_FLOOR, (
            f"1/32 的占比算出 {waste:.4f}，没低过线 {DEGENERATE_WASTE_FLOOR}"
            "——阈值失去分辨力，这条测试就不再证明任何事"
        )

    def test_a_healthy_config_does_not_fire_it(self) -> None:
        waste = self._b0_waste(n_prefixes=32, n_aliased=8)
        assert waste > DEGENERATE_WASTE_FLOOR

    def test_the_margin_is_at_least_five_times_the_container_tolerance(self) -> None:
        """线本身是**从容差推出来的**：容器侧「回包计数差 ≤ 1%」（`:739`）
        是这个比率端点的噪声地板，信号至少要有它的 5 倍。"""
        assert pytest.approx(DEGENERATE_WASTE_FLOOR, rel=1e-9) == 0.05
        assert self._b0_waste(32, 8) / 0.01 >= 5


# ── 真的跑一遍（T13 的 Verify 本体）─────────────────────────────────────────


def _config_prefixes() -> list[str]:
    """config 里的 32 个前缀，**按书写顺序**、取规范形式。

    顺序必须与 config 一致——runner 就是这样把它们交给策略的，而
    `test_stage1_config.py` 已经断言 `read_subnets` 保持那个顺序。
    """
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    return [
        str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        for entry in document["subnets"]
    ]


def _prefix_of(addr: str, prefixes) -> str | None:
    """最长前缀匹配——与靶场的 trie 同一条规则（`score.py` 的归属规则）。"""
    ip = ipaddress.ip_address(addr)
    best: ipaddress.IPv6Network | None = None
    for raw in prefixes:
        net = ipaddress.ip_network(raw, strict=False)
        if ip in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best) if best is not None else None


class _RecordingExecutor:
    """不出网卡；回包按**真值**给，并把每一条 `Response` 按序记下来。

    **回包内容不影响 B0 的任何一列**——它不看观测。所以这个简化靶场不会让两个
    共主端点的数字失真：`aliased_waste_ratio` 只取决于探测花在哪，
    `aliased_caught` 恒为 0（B0 不声明）。真实靶场的速率分布要到 T16 跑冒烟表时
    才参与——**这里刻意不模拟它**，因为一个"照 config 速率掷骰子"的假靶场会把
    一份我们没验证过的仿真结论混进 T13 的验收里。
    """

    def __init__(self, truth) -> None:
        self._truth = truth
        self.responses: list[Response] = []

    def run(self, targets) -> ProbeRun:
        out: list[Response] = []
        for target in targets:
            dst = target.dst if hasattr(target, "dst") else str(target)
            matched = _prefix_of(dst, self._truth.prefixes)
            aliased = bool(matched and self._truth.prefixes[matched].get("aliased"))
            out.append(
                Response(status="received", dst=dst, src=dst, hop_limit=64, rtt_ms=1.0)
                if aliased
                else Response(status="sent_no_reply", dst=dst)
            )
        self.responses.extend(out)
        return ProbeRun(responses=tuple(out), sent=len(out))


def _sampler(prefix: str, n: int) -> list[str]:
    """前缀里的 `n` 个地址。B0 不看它们是什么，但 `_require_in_scope` 会看。"""
    net = ipaddress.ip_network(prefix, strict=False)
    return [str(net.network_address + i + 1) for i in range(n)]


class TestB0ThroughTheRealRunner:
    """**T13 的 Verify：「B0 单独跑一遍」。**

    跑的是完整那条路：真子进程 → 握手 → 真的 `SurfaceServer` + 权威账本 →
    真的 config 当真值 → 真的 `score()`。四层各有一条断言。

    为什么这件事值得这样一个测试，而不是"看一眼数"：B0 的分数是**事先可算**的
    （`0.25` / `0`），所以这里能写成两条**精确**断言。**算出来的数与跑出来的数
    相等**，这件事同时验证了分配、握手、账本、归属规则和端点定义——任何一层
    偏一点，等号就不成立。
    """

    BUDGET = 300
    #: 这一对 `(策略, seed)` 的 seed（T6a / 协议 v2）。B0 不用随机源，但它仍
    #: 必须走这条通道——握手**无条件**发种子，没有「这个策略不需要就不发」
    #: 那一支（见 `SurfaceServer.__init__` 的 `seed` 参数）。
    SEED = 0

    def _session(self):
        truth = stage1_truth_from_config(CONFIG_PATH)
        prefixes = _config_prefixes()
        executor = _RecordingExecutor(truth)
        server = SurfaceServer(
            executor=executor,
            oracle=Oracle(truth_source=lambda: truth, reset_target=lambda seed: None),
            ledger=BudgetLedger(self.BUDGET),
            sampler=_sampler,
            prefixes=prefixes,
            seed=self.SEED,
            epoch_size=20,
        )
        process = spawn_strategy(
            "src.baselines.b0_probe_all:run", server=server, cwd=str(REPO_ROOT)
        )
        return server, executor, truth, process

    def test_the_session_runs_clean_and_spends_the_whole_budget(self) -> None:
        server, executor, _, process = self._session()
        assert process.wait(timeout=120) == 0, process.stderr()
        assert server.ledger.left == 0, "预算没花完——B0 应该恰好花光"
        assert len(executor.responses) == self.BUDGET
        assert server.probes_run == self.BUDGET

    def test_the_measured_waste_ratio_is_exactly_the_predicted_quarter(self) -> None:
        """**预言 0.25，实测必须是 0.25。**

        这个等号是 T13 最值钱的一条：B0 均分给每个前缀、aliased 是列表前 1/4、
        而累进除法对任何连续 1/4 段给的都是总数的 1/4。**只要有一层把顺序、
        归属或分母弄偏，等号就不成立。**
        """
        server, executor, truth, process = self._session()
        assert process.wait(timeout=120) == 0, process.stderr()
        run = RunRecord(probes=tuple(executor.responses), reports=tuple(server.reports))
        result = score(run, truth)

        assert result.probes_charged == self.BUDGET
        assert result.aliased_waste_ratio == pytest.approx(0.25, abs=1e-12)

    def test_the_alarm_does_not_fire_and_the_structural_zero_holds(self) -> None:
        """两条一起测，因为它们是**同一件事的两面**：B0 的 `aliased_caught`
        是构造性 0（`:508` 要求表注写明这一点），而这个 0 与 `report()` 通道
        的沉默是同一事实。"""
        server, executor, truth, process = self._session()
        assert process.wait(timeout=120) == 0, process.stderr()
        run = RunRecord(probes=tuple(executor.responses), reports=tuple(server.reports))
        result = score(run, truth)

        assert server.reports == [], "B0 声明了——它没有判定环节，不该有声明"
        assert result.aliased_caught == 0
        assert result.aliased_false_positives == 0
        assert result.aliased_misses == 8, "八个 aliased 前缀一个都没被声明"
        assert result.aliased_waste_ratio > DEGENERATE_WASTE_FLOOR, (
            f"退化警报响了：实测 {result.aliased_waste_ratio} 不高于线 "
            f"{DEGENERATE_WASTE_FLOOR}——config 要重设（D10）"
        )

    def test_the_prediction_and_the_measurement_cannot_drift_apart(self) -> None:
        """把「事先算的」与「跑出来的」**放在同一个断言里**。

        分开写的话，改 `plan_counts` 只会让其中一条红，而另一条仍然绿——那时
        看的人会以为是"预言过时了"，而实际上是实现变了。
        """
        _, executor, truth, process = self._session()
        assert process.wait(timeout=120) == 0, process.stderr()
        run = RunRecord(probes=tuple(executor.responses), reports=())
        measured = score(run, truth).aliased_waste_ratio

        table = read_subnets(CONFIG_PATH)
        counts = plan_counts(tuple(_config_prefixes()), self.BUDGET)
        predicted = sum(counts[: len(table.aliased)]) / self.BUDGET
        assert measured == pytest.approx(predicted, abs=1e-12)
