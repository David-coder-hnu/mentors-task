"""`B4`「响应率自适应 bandit」—— 冒烟表的记忆族无记忆控制组，也是 **trap 曲线**
的驱动者（T6a / `:848`）。

这组测试钉四件事：

1. **轮数节奏**（`TestEpochSize` / `TestRoundSizes`）——`M2 vs B4` 是冒烟表的
   主比较（`:638`），再决策的**频率**不得成为那个差里混进来的自变量。
2. **UCB1 的行为**（`TestPlanRound`）——没探过的臂先探、高分臂后面拿大头、
   平局随机。最后一条不是洁癖：config 里 8 个 aliased 恰好排在最前面，
   一条「取最小下标」的平局规则会白送 B4 一部分识别率。
3. **trap 真的会出现**（`TestTheTrapIsReal`）——`aliased_waste_ratio` 必须
   **远高于** B0 的 0.25。原稿里没有任何策略会「按响应率分配预算」，而 trap
   故事恰恰由它驱动；没有 B4，那条交叉线在实验里根本不会出现。这条是那句话的
   可执行版本，**实测数写在断言的 docstring 里**，不是估的。
4. **声明规则**（`TestDeclaration`）——走 `declarations()` 这条**真路径**
   （把它从 `run()` 末尾抽出来的唯一理由就是可测），两个条件各有一个反面用例。

最后一组跑**真子进程**，那是 **T6a 的种子通道**在 B4 上的验收（B1 是第一家，
见 `test_b1.py`）：同 seed ⇒ 逐前缀相同。
"""

from __future__ import annotations

import ipaddress
import json
import math
import random
from pathlib import Path

import pytest

from src.adapter.ground_truth import stage1_truth_from_config
from src.adapter.oracle import Oracle
from src.adapter.probe_pool import ProbeRun
from src.adapter.runner import BudgetLedger, SurfaceServer, spawn_strategy
from src.adapter.types import Response
from src.baselines.b4_response_bandit import (
    EPOCH_SIZE,
    REPEAT_FLOOR,
    UCB1_C,
    declarations,
    plan_round,
    round_sizes,
    run,
)
from src.scoring.score import RunRecord, score

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "stage1_config.json"
ENTRY = "src.baselines.b4_response_bandit:run"


# ── 假件 ────────────────────────────────────────────────────────────────────


class _Answer:
    """`Answered` 协议的最小实现 —— 只有 `status`。"""

    __slots__ = ("status",)

    def __init__(self, status: str) -> None:
        self.status = status


class FakeSurface:
    """一个**按给定概率**回包的假 surface。

    它比 `test_b0.py` / `test_b1.py` 的同名假件多两样，两样都是 B4 逼出来的：

    * **`report`** —— B4 会声明，而声明是 `aliased_caught` 的唯一来源。
    * **回包** —— B4 的全部行为都由「回了没有」驱动。一个不回包的假靶场会让它
      退化成 B1 的形状（只有探索、没有利用），而那正是要测的反面。

    两个随机源刻意分开：**策略那边**走 `rng()`（与真会话同一条种子派生，
    所以「同 seed ⇒ 同一条规划」在这个假件里也成立），**回包那边**走
    `seed + rng_offset`——于是 `rng_offset` 能在同一份种子的规划下造出另一条
    回包轨迹，「哪一半不对」因此是可分的，而不是笼统地「不可复现」。
    """

    def __init__(
        self,
        prefixes: list[str],
        budget: int | None,
        rates: dict[str, float],
        *,
        seed: int = 0,
        rng_offset: int = 1000,
    ) -> None:
        self.prefixes = tuple(prefixes)
        self._budget = budget
        self._rates = rates
        self._seed = seed
        self._rng_offset = rng_offset
        self._responses: random.Random | None = None
        self.calls: list[tuple[str, int]] = []
        self.reports: list[tuple[str, bool]] = []

    @property
    def budget_left(self) -> int | None:
        return self._budget

    def sample(self, prefix: str, n: int) -> list[_Answer]:
        self.calls.append((prefix, n))
        rate = self._rates[prefix]
        return [
            _Answer("received" if self._response_rng().random() < rate else "sent_no_reply")
            for _ in range(n)
        ]

    def report(self, prefix: str, aliased: bool) -> None:
        self.reports.append((prefix, aliased))

    def rng(self) -> random.Random:
        """与 `ProbeSurface.rng()` 同形：**每次返回一个新的 `Random`**（种子决定一切）。"""
        return random.Random(self._seed)

    def _response_rng(self) -> random.Random:
        if self._responses is None:
            self._responses = random.Random(self._seed + self._rng_offset)
        return self._responses

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {p: 0 for p in self.prefixes}
        for prefix, n in self.calls:
            out[prefix] += n
        return out

    def spent(self) -> int:
        return sum(n for _, n in self.calls)


def _prefixes(n: int) -> list[str]:
    return [f"2001:db8:5:{i:x}::/64" for i in range(n)]


def _rates(prefixes: list[str], aliased: int, background: float) -> dict[str, float]:
    """前 `aliased` 个恒回包，其余以 `background` 回包。

    `background = 0.0` 让整条轨迹**完全确定**（回包不掷骰子），于是那张表量的是
    B4 的规划而不是假靶场的噪声；`0.3` 那一档另有一条测试，证明结论不依赖这个取值。
    """
    return {p: (1.0 if i < aliased else background) for i, p in enumerate(prefixes)}


# ── 节奏 ────────────────────────────────────────────────────────────────────


class TestEpochSize:
    """`EPOCH_SIZE` 是 config 里 `epoch_size` 的**第二个副本**——这里把两份钉住。"""

    def test_the_epoch_size_matches_the_config(self) -> None:
        """`epoch_size` 今天**没有任何通道进策略进程**：握手只带 `prefixes` /
        `seed` / 预算视图（`runner.py:164`）。所以 B4 只能抄一份常量，而抄来的
        常量必须有一条测试盯着——否则改 config 而不改这里，B4 会静默地换一套
        节奏，而「主比较里混进了再决策频率」这件事**不会以任何形式报错**。
        """
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        assert EPOCH_SIZE == document["epoch_size"], (
            f"B4 的 EPOCH_SIZE={EPOCH_SIZE} 与 config 的 "
            f"epoch_size={document['epoch_size']} 不一致——B4 的节奏会静默地与 "
            "M1/M2 错开，而 M2 vs B4 是冒烟表的主比较"
        )

    def test_the_smoke_budget_yields_fifteen_rounds(self) -> None:
        """300 / 20 = 15 轮，与 `test_stage1_config.py` 断言的 epoch 数一致。"""
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        budget = document["budget"]
        assert budget // EPOCH_SIZE == budget // document["epoch_size"] == 15

    def test_the_exploration_constant_is_the_textbook_one(self) -> None:
        """`sqrt(2)` 是 UCB1 的定义（Auer et al. 2002 的 α=2），**不是调出来的**。

        任何在评测拓扑上调出来的常数都会让 B4 与 B5 站在不同的标准上，
        而 B5 的公平性协议（`:643`）管的就是这件事。
        """
        assert UCB1_C == pytest.approx(math.sqrt(2.0))


class TestRoundSizes:
    """累进除法：总和恰好是预算，余数摊平。"""

    def test_the_sizes_telescope_to_the_budget(self) -> None:
        for budget, rounds in ((300, 15), (300, 7), (100, 3), (301, 10), (20, 1)):
            sizes = round_sizes(budget, rounds)
            assert sum(sizes) == budget, f"{budget}/{rounds} 摊下来是 {sum(sizes)}，不是 {budget}"
            assert len(sizes) == rounds
            assert all(size >= 1 for size in sizes)

    def test_the_remainder_is_not_dumped_on_the_last_round(self) -> None:
        """同 `b0_probe_all.plan_counts` 的理由：余数全压最后一轮会让那个 epoch
        明显更长，而 epoch 长度是排空窗口 `W` 的因子（`:586`）。"""
        sizes = round_sizes(301, 10)
        assert max(sizes) - min(sizes) <= 1, f"{sizes} 的极差超过 1，余数没摊平"
        assert sizes[-1] <= sizes[0] + 1

    def test_a_budget_smaller_than_the_round_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="盖不住"):
            round_sizes(3, 10)

    def test_zero_rounds_is_refused(self) -> None:
        with pytest.raises(ValueError, match="至少要一轮"):
            round_sizes(300, 0)


# ── 规划 ────────────────────────────────────────────────────────────────────


class TestPlanRound:
    """UCB1 本身。每条断言都对应一种会静默的写法。"""

    K = 4

    def _plan(self, pulls, rewards, size, seed=0):
        return plan_round(pulls, rewards, size, random.Random(seed))

    def test_unvisited_arms_are_probed_first(self) -> None:
        """没探过的臂记 `+inf`（否则第一次除零）。**它必须排在所有有限分数前面**
        ——4 个臂全未探过、规划 4 次，每个臂恰好拿到 1 次。"""
        plan = self._plan([0, 0, 0, 0], [0.0, 0.0, 0.0, 0.0], self.K)
        assert plan == (1, 1, 1, 1), (
            f"规划是 {plan}——未探过的臂没有被优先探完，而这是 UCB1 的定义"
        )

    def test_a_high_response_arm_takes_the_bulk(self) -> None:
        """**这就是 trap。** 8 个臂里前 2 个恒回包、其余从不回包，各已探 4 次、
        再规划 40 次：大头必须落在前两个上。

        （「其余从不回包」是 `OfflineExecutor` 的口径。真实 config 的 0.3 / 0.1
        那些档会让分配温和一些但方向不变——`TestTheTrapIsReal` 量的是那个差。）
        """
        plan = self._plan([4] * 8, [4.0, 4.0] + [0.0] * 6, 40)
        assert sum(plan[:2]) > sum(plan[2:]), (
            f"规划 {plan}——恒回包的两个臂没拿到大头，trap 曲线画不出来"
        )
        assert sum(plan[:2]) >= 20, (
            f"规划 {plan}——高分臂只拿到 {sum(plan[:2])}/40，比预期温和太多"
        )

    def test_the_plan_totals_the_round_size(self) -> None:
        for size in (1, 3, 20, 137):
            plan = self._plan([1, 2, 3, 4], [1.0, 1.0, 0.5, 0.0], size)
            assert sum(plan) == size

    def test_it_does_not_mutate_its_inputs(self) -> None:
        """`plan_round` **不改动入参**——「统计只在轮边界更新」这条契约因此在类型上
        成立：调用方不接住返回值就什么也不会发生（虚拟拉杆必须先拷贝）。
        """
        pulls = [1, 0, 3]
        rewards = [1.0, 0.0, 0.0]
        self._plan(pulls, rewards, 10)
        assert pulls == [1, 0, 3]
        assert rewards == [1.0, 0.0, 0.0]

    def test_ties_are_broken_by_the_rng(self) -> None:
        """全对称的状态下，平面**规则的顺序**由随机源决定，而不是由下标决定。

        **第一版这条测试是没有牙的，值得写下来**：8 个臂统计完全相同、规划 8 次时
        不管平局怎么破，得到的**分配都是 `(1,1,...,1)`**——分配在完全对称的输入上
        是顺序无关的，于是那条断言在「平局随机」与「平局取最小下标」两种实现下
        都绿。要看平局规则，必须看**一轮的大小小于平局集合**的那种轮：这里取
        2 个臂、规划 1 次，结果只能是 `(1,0)` 或 `(0,1)`，而「取最小下标」会
        永远给出 `(1,0)`。

        用 8 个臂、规划 1 次、200 个种子再补一层：**每个下标都要被选中过**。
        config 里 aliased 恰好排在前面 8 个，所以「下标 0 有系统性优势」这件事
        会让 B4 的识别率里混进一份来自 config 行序的红利。
        """
        picked = {
            self._plan([0] * 8, [0.0] * 8, 1, seed=seed).index(1) for seed in range(200)
        }
        assert picked != {0}, (
            "200 个种子全部选中了下标 0——平局没有随机化，"
            "config 的行序正在给列表前部送红利"
        )
        assert picked == set(range(8)), (
            f"200 个种子只探到下标 {sorted(picked)}——"
            "平局分布不均匀，某些臂在探索轮里拿不到机会"
        )

    def test_a_symmetric_state_still_uses_the_rng_on_the_second_pass(self) -> None:
        """第二轮：全部探过 1 次、全部 0 回报（统计完全对称）。这里也必须随机。

        它防的是「第一轮随机、之后一律取最小下标」这类只在循环外随机的写法——
        那种写法下 `picked` 会是 `{0}`。
        """
        picked = {
            self._plan([1] * 8, [0.0] * 8, 1, seed=seed).index(1) for seed in range(200)
        }
        assert picked == set(range(8)), (
            f"第二轮只探到下标 {sorted(picked)}——平局在第二轮没有随机化"
        )

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="长度不同"):
            self._plan([1, 2], [1.0], 3)

    def test_an_empty_arm_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="没有臂"):
            self._plan([], [], 3)

    def test_a_non_positive_round_size_is_refused(self) -> None:
        with pytest.raises(ValueError, match="至少要 1 次探测"):
            self._plan([1], [1.0], 0)


# ── trap 真的会出现吗 ───────────────────────────────────────────────────────


class TestTheTrapIsReal:
    """**这条是 B4 存在的理由**（`:643`）。B0 把预算均摊（waste = 8/32 = 0.25），
    B4 必须显著高于它。"""

    N = 32
    ALIASED = 8
    BUDGET = 300
    #: B0 的同一个数，构造上精确。
    B0_RATIO = ALIASED / N

    def _run(self, *, aliased: int, background: float, seed: int = 0) -> FakeSurface:
        prefixes = _prefixes(self.N)
        surface = FakeSurface(
            prefixes, self.BUDGET, _rates(prefixes, aliased, background), seed=seed
        )
        run(surface)  # type: ignore[arg-type]
        return surface

    def _ratio(self, surface: FakeSurface, aliased: int) -> float:
        counts = surface.counts()
        return sum(counts[p] for p in surface.prefixes[:aliased]) / self.BUDGET

    def test_pure_trap_pours_two_thirds_of_the_budget_into_aliased(self) -> None:
        """恒回包的 8 个前缀 vs 从不回包的 24 个：**实测 0.6667，5 个 seed 一模一样**
        （每个 aliased 各 25 次 = 200，其余各 4–5 次 = 100）。

        与 UCB1 的平衡点算出来的一致：解 `1 + sqrt(2 ln N / n_a) = sqrt(2 ln N / n_o)`
        在 `N = 300` 下给 `n_a ≈ 25`、`n_o ≈ 4.2`。**界取 0.6 而不是 0.5**——
        0.5 太靠近那个平衡点的下沿，而这里没有理由把界放到刚好过线。
        """
        ratios = [
            self._ratio(self._run(aliased=self.ALIASED, background=0.0, seed=s), self.ALIASED)
            for s in range(5)
        ]
        assert min(ratios) > 0.6, (
            f"五个 seed 的 waste ratio 是 {[round(r, 4) for r in ratios]}——"
            "B4 没有把预算倒进恒回包的前缀，trap 曲线画不出来"
        )
        assert all(r > self.B0_RATIO for r in ratios)

    def test_a_thirty_percent_background_is_still_a_trap(self) -> None:
        """非 aliased 的那 24 个以 **0.3** 回包（= config 的 content-mid 档）时实测
        **0.5133 – 0.5667**。

        有这一条，上面那个 0.6667 就不是「只有靶场完全不回包时才成立」的结论。
        **界取 0.4**，离实测下沿还有 0.11 的余量。
        """
        ratios = [
            self._ratio(self._run(aliased=self.ALIASED, background=0.3, seed=s), self.ALIASED)
            for s in range(5)
        ]
        assert min(ratios) > 0.4, (
            f"五个 seed 的 waste ratio 是 {[round(r, 4) for r in ratios]}——"
            "0.3 的背景响应率下 B4 没有把预算倒向 aliased 前缀"
        )
        assert all(r > self.B0_RATIO for r in ratios)

    def test_b0s_number_is_exactly_a_quarter(self) -> None:
        """对照的锚点：8/32 = 0.25。上面两条都是相对它在说「高出一截」。"""
        assert self.B0_RATIO == pytest.approx(0.25)

    def test_no_aliased_prefix_means_no_waste(self) -> None:
        """**反方向，而且是最要紧的一条**：aliased 数为 0 时 waste ratio 必须恰好是 0
        （实测 0.0000，5 个 seed 都是）。

        没有它，一个「无脑全投第一个前缀」的实现也能过上面那两条——而那个实现
        在 trap 上看起来完全正确。
        """
        surface = self._run(aliased=0, background=0.3)
        assert self._ratio(surface, 0) == 0.0

    def test_it_spends_the_budget_exactly(self) -> None:
        surface = self._run(aliased=self.ALIASED, background=0.0)
        assert surface.spent() == self.BUDGET

    def test_zero_counts_are_never_sent(self) -> None:
        """`sample(prefix, 0)` 会被 runner 拒（`runner.py:177`），所以必须跳过。"""
        surface = self._run(aliased=self.ALIASED, background=0.0)
        assert all(n >= 1 for _, n in surface.calls), (
            f"发出去了 0 次的请求 {[c for c in surface.calls if c[1] < 1]}——"
            "现场是跑到一半的 SurfaceError"
        )

    def test_every_prefix_is_probed_at_least_once(self) -> None:
        """**这条不是「顺手」**：`pulls == 0` 的前缀不会被声明（第五节），所以
        「全部 32 个都被探过」是「32 条声明」与 `aliased_misses` 可读的前提。
        """
        surface = self._run(aliased=self.ALIASED, background=0.0)
        assert all(n >= 1 for n in surface.counts().values())

    def test_the_rounds_are_fifteen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**轮数是可数的**：数 `plan_round` 被调了几次，以及每轮多大。

        **第一版这条测试是没有牙的，值得写下来**：它从模块常量算出「每轮 20」，
        再用这个期望去切 `surface.calls`，于是 `rounds` 那个计数器**完全由期望
        驱动**——把 `rounds = budget // EPOCH_SIZE` 改成 `len(prefixes)` 之后，
        它照样数出 15。变异检验抓到的就是这个。

        现在改成**观测调用本身**：`run()` 是按模块全局名调 `plan_round` 的，
        所以换成计数桩能如实看到决策发生了多少次。
        """
        import src.baselines.b4_response_bandit as b4

        sizes: list[int] = []
        real = b4.plan_round

        def spy(pulls, rewards, round_size, rng):
            sizes.append(round_size)
            return real(pulls, rewards, round_size, rng)

        monkeypatch.setattr(b4, "plan_round", spy)

        surface = self._run(aliased=self.ALIASED, background=0.0)
        assert sizes == list(round_sizes(self.BUDGET, self.BUDGET // EPOCH_SIZE)), (
            f"实际决策了 {len(sizes)} 轮（每轮 {sorted(set(sizes))}），期望 15 轮 × 20"
        )
        assert sum(sizes) == self.BUDGET == surface.spent()


# ── 声明 ────────────────────────────────────────────────────────────────────


class TestDeclaration:
    """`aliased_caught` 的唯一来源是 `report()`（`:488`），所以规则必须可测。

    测的是 `declarations()` —— **`run()` 末尾调的就是它**（「抽出来」这件事的
    全部理由就是可测）。规则的两半各有反面用例：只测正面的话，一个「恒报 True」
    的实现照样绿，而那正是 `score.py` 模块 docstring 点名的那个博弈路径。
    """

    def test_the_repeat_floor_is_two(self) -> None:
        """`REPEAT_FLOOR` 是**非平凡的最小数**，不是调出来的阈值：只探一次就下结论
        等于把「这一次恰好回了」当签名，而 config 里 0.3 那一档有 30% 的概率恰好回。
        """
        assert REPEAT_FLOOR == 2

    def test_always_answered_over_enough_pulls_is_declared(self) -> None:
        assert declarations(["p"], [5], [5]) == [("p", True)]

    def test_a_single_miss_clears_it(self) -> None:
        """真前缀的响应率严格小于 1（config 最高的一档是 0.3），所以它**迟早会漏**。
        一次不漏地回很多次才是签名；漏过一次就不是了。"""
        assert declarations(["p"], [5], [4]) == [("p", False)]

    def test_one_pull_is_not_enough(self) -> None:
        """只探过一次、恰好回了 —— **不下判定**。"""
        assert declarations(["p"], [1], [1]) == [("p", False)]

    def test_exactly_the_repeat_floor_is_enough(self) -> None:
        """边界另一侧：恰好 `REPEAT_FLOOR` 次且全回 ⇒ 判定成立。

        与上一条配对——只有上一条的话，把 `>=` 写成 `>` 也一样绿。
        """
        assert declarations(["p"], [2], [2]) == [("p", True)]

    def test_an_unprobed_prefix_is_not_declared_at_all(self) -> None:
        """没有证据就没有判断。报成 `False` 会让 `aliased_misses` 少算一个
        「没识别出来」的前缀，而那一列要说的事正是这个。"""
        assert declarations(["p"], [0], [0]) == []

    def test_a_mixed_batch_keeps_the_prefix_order(self) -> None:
        got = declarations(["a", "b", "c", "d"], [0, 3, 4, 2], [0, 3, 3, 2])
        assert got == [("b", True), ("c", False), ("d", True)]

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="长度不一致"):
            declarations(["a", "b"], [1], [1, 1])

    def test_the_session_declares_every_prefix_it_examined(self) -> None:
        """真会话里核对：声明覆盖的恰好是「探过至少一次」的前缀，恒回包的 8 个被判
        True、其余判 False（0.0 背景下没有假阳性）。"""
        prefixes = _prefixes(32)
        surface = FakeSurface(prefixes, 300, _rates(prefixes, 8, 0.0), seed=0)
        run(surface)  # type: ignore[arg-type]

        declared = dict(surface.reports)
        counts = surface.counts()
        assert set(declared) == {p for p in prefixes if counts[p] > 0}
        assert all(declared[p] for p in prefixes[:8]), "恒回包的 8 个前缀没被判 aliased"
        assert not any(declared[p] for p in prefixes[8:]), (
            "从不回包的前缀被判成了 aliased——声明规则反了"
        )

    def test_the_reports_are_not_flip_flopping(self) -> None:
        """同一前缀只声明一次。声明是**最终判断**（`score.py`：同前缀多次声明取后者），
        每轮报一次不改变得分，只会往 `report_flip_flops` 里灌噪声——而那一列的用途
        是标记**不确定**的策略，不是标记**勤奋**的策略。"""
        prefixes = _prefixes(32)
        surface = FakeSurface(prefixes, 300, _rates(prefixes, 8, 0.0), seed=0)
        run(surface)  # type: ignore[arg-type]
        assert len(surface.reports) == len({p for p, _ in surface.reports})


# ── 真的跑一遍（T6a 的种子通道验收）─────────────────────────────────────────


def _config_prefixes() -> list[str]:
    """与 `ground_truth._normalize_prefix` 同一套规范化；顺序照 config（前 8 个是 aliased）。"""
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    return [
        str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        for entry in document["subnets"]
    ]


def _prefix_of(addr: str, prefixes) -> str | None:
    """与 `score._match_address` 同一条规则：最长前缀匹配。"""
    ip = ipaddress.ip_address(addr)
    best: ipaddress.IPv6Network | None = None
    for raw in prefixes:
        net = ipaddress.ip_network(raw, strict=False)
        if ip in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best) if best is not None else None


class _TruthExecutor:
    """不出网卡；**只给 aliased 前缀回包，其余一律不回**。

    这个口径与 `src/experiment/executors.py::OfflineExecutor` 一致，理由也一样：
    一个「照 config 速率掷骰子」的假靶场会把一份我们没验证过的仿真结论混进验收里。
    于是这条测试证明的是 **trap 的方向与量级**（B4 把 2/3 的预算倒进恒回包的前缀），
    而真实靶场上 0.3 / 0.1 / 0.02 各档的确切数要让 T16 去跑。
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

    def per_prefix(self, prefixes) -> dict[str, int]:
        counts: dict[str, int] = {p: 0 for p in prefixes}
        for response in self.responses:
            matched = _prefix_of(response.dst, prefixes)
            if matched is not None:
                counts[matched] += 1
        return counts


class TestB4ThroughTheRealRunner:
    """**T6a 的种子通道在 B4 上的验收**：真子进程、真握手、真账本、真评分器。

    与 `test_b1.py` 那一组同形。B4 比 B1 多一层证据：它**自适应**，所以轨迹依赖
    观测，而观测依赖随机源——「同 seed ⇒ 逐前缀相同」在这里同时验证了握手把种子
    送到了、以及策略**没有**另起一个随机源。
    """

    BUDGET = 300
    ALIASED = 8

    def _session(self, seed: int):
        truth = stage1_truth_from_config(CONFIG_PATH)
        prefixes = _config_prefixes()
        executor = _TruthExecutor(truth)
        server = SurfaceServer(
            executor=executor,
            oracle=Oracle(truth_source=lambda: truth, reset_target=lambda seed: None),
            ledger=BudgetLedger(self.BUDGET),
            sampler=lambda prefix, n: [
                f"{prefix.split('/')[0].rstrip(':')}::{i + 1:x}" for i in range(n)
            ],
            prefixes=prefixes,
            seed=seed,
        )
        process = spawn_strategy(ENTRY, server=server, cwd=str(REPO_ROOT))
        return server, executor, truth, prefixes, process

    def test_the_session_runs_clean_and_spends_the_whole_budget(self) -> None:
        server, executor, _, _, process = self._session(seed=0)
        assert process.wait(timeout=120) == 0, process.stderr()
        assert server.ledger.left == 0, "预算没花完——B4 应该恰好花光"
        assert len(executor.responses) == self.BUDGET

    def test_the_same_seed_gives_the_same_per_prefix_counts(self) -> None:
        """**T6a 的核心断言，在 B4 上。**

        比较的是逐前缀探测数，不是总分：在这个确定性靶场上总分也是确定的，但它是
        一个标量，而两条不同的分配折出同一个比是可能的（`test_b1.py` 里那条关于
        2.5% 噪声的推理就是这个道理）。
        """
        _, first, _, prefixes, process = self._session(seed=21)
        assert process.wait(timeout=120) == 0, process.stderr()
        _, second, _, prefixes_again, process = self._session(seed=21)
        assert process.wait(timeout=120) == 0, process.stderr()

        a, b = first.per_prefix(prefixes), second.per_prefix(prefixes_again)
        assert a == b, (
            "同一个 seed 跑出两条不同的覆盖——B4 里另起了一个随机源，"
            f"或者种子没进到 `surface.rng()`。不同的前缀：{[p for p in a if a[p] != b[p]][:5]}"
        )
        assert len(set(a.values())) > 1, (
            "所有前缀拿到了一样多次——这看起来像 B0，不像自适应 bandit"
            "（也可能是分配退化成常量，那样上面那条会永远通过）"
        )

    def test_a_different_seed_gives_a_different_coverage(self) -> None:
        """反方向：**无视种子的实现**（比如恒等分配）能过上面那条，过不了这条。"""
        _, first, _, prefixes, process = self._session(seed=21)
        assert process.wait(timeout=120) == 0, process.stderr()
        _, second, _, prefixes_again, process = self._session(seed=22)
        assert process.wait(timeout=120) == 0, process.stderr()
        assert first.per_prefix(prefixes) != second.per_prefix(prefixes_again)

    def test_it_catches_the_aliased_prefixes_and_scores_the_trap(self) -> None:
        """**一条会话同时验三样**：声明通道通、`aliased_caught` 到顶、
        `aliased_waste_ratio` 高出 B0 的 0.25。

        恒回包的前缀在 UCB1 下拿到约 25 次探测（`TestTheTrapIsReal` 量过），远超
        `REPEAT_FLOOR`，所以声明必然触发；而非 aliased 的拿 4–5 次、一次都不回，
        所以不会被误判。**界取 0.5**：这里比假件那一组薄一层（多了一次
        `sampler → 地址 → 最长前缀匹配` 的往返），留一点余量。
        """
        server, executor, truth, _, process = self._session(seed=5)
        assert process.wait(timeout=120) == 0, process.stderr()
        run_record = RunRecord(
            probes=tuple(executor.responses),
            reports=tuple(server.reports),
        )
        result = score(run_record, truth)

        assert result.aliased_caught == self.ALIASED, (
            f"只认出 {result.aliased_caught}/{result.aliased_prefixes_total} 个 aliased "
            "前缀——声明通道或规则没生效"
        )
        assert result.aliased_false_positives == 0, (
            "这条假靶场里非 aliased 的前缀一次都不回包，出现了假阳性说明"
            "回包与归属对不上"
        )
        assert result.aliased_waste_ratio > 0.5, (
            f"waste ratio {result.aliased_waste_ratio:.4f} 没高过 B0 的 0.25 一截——"
            "B4 没有把预算倒进 aliased 前缀"
        )
        assert result.report_flip_flops == 0, "同一前缀被报了两次以上"

    def test_every_prefix_gets_probed_so_every_prefix_gets_declared(self) -> None:
        """`SurfaceServer.reports` 是 `aliased_caught` 的唯一来源（`score.py:377`），
        所以声明必须在**服务端**留痕，而不只是在策略进程里记了一笔。

        32 条 = 每个前缀一条：UCB1 的 `+inf` 会把每个没探过的臂都探一次，于是
        「探过的」= 全部 32 个。
        """
        server, _, _, prefixes, process = self._session(seed=5)
        assert process.wait(timeout=120) == 0, process.stderr()
        assert len(server.reports) == 32, (
            f"服务端只收到 {len(server.reports)} 条声明，期望 32（每个前缀一条）"
        )
        declared_true = {p for p, aliased in server.reports if aliased}
        assert declared_true == set(prefixes[: self.ALIASED]), (
            "被判 aliased 的集合与 config 前 8 个不一致——"
            f"多出来的：{sorted(declared_true - set(prefixes[: self.ALIASED]))[:3]}"
        )
