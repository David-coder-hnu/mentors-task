"""`B1`「均匀随机」—— 冒烟表的下界（T6a）。

这组测试钉两件事，各自对应一类静默失效：

1. **分配是均匀的**（`TestPlanCounts`）。均匀是 B1 的**全部定义**——它没有反馈、
   没有判定，唯一能测的就是「这些包撒得对不对」。所以既测总量恰好等于预算，
   也测它只用 `random()`（跨版本稳定那一条），还测小预算下真的会跳过前缀。
2. **它真的能跑起来**（`TestB1ThroughTheRealRunner`）。这是 **T6a 的种子通道**
   的验收：B1 是 `surface.rng()` 的第一个用户，而那条通道此前不存在——
   `driver.py` 的「今天能跑什么」表里 B1 那一栏写的是「跑不了」。
   这里跑的是**真子进程**（`spawn_strategy` → `strategy_child` → 握手 → `b1_uniform_random:run`），
   而 **同 seed 跑两遍必须逐前缀相同**——那是「可复现随机源」这句话的全部内容。
"""

from __future__ import annotations

import ipaddress
import json
import random
from pathlib import Path

import pytest

from src.adapter.ground_truth import stage1_truth_from_config
from src.adapter.oracle import Oracle
from src.adapter.probe_pool import ProbeRun
from src.adapter.runner import BudgetLedger, SurfaceServer, spawn_strategy
from src.adapter.types import Response
from src.baselines.b1_uniform_random import plan_counts, run
from src.scoring.score import RunRecord, score

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "stage1_config.json"
ENTRY = "src.baselines.b1_uniform_random:run"


# ── 假件 ────────────────────────────────────────────────────────────────────


class _OnlyRandom:
    """只暴露 `random()` 的随机源替身。

    **它是一条有牙的断言。** `ProbeSurface.rng()` 返回的是 `random.Random`，
    而那个对象上还有 `randrange` / `shuffle` / `sample` / `randint` 四个方法，
    其中**没有一个**被官方保证跨版本稳定（见 `surface.rng` 的第一条规矩）。
    把这个替身喂给 `plan_counts`，谁改用那四个里的任一个都会当场 `AttributeError`
    ——比在 docstring 里写一句「请只用 random()」牢靠。
    """

    def __init__(self, inner: random.Random) -> None:
        self._inner = inner
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self._inner.random()


class FakeSurface:
    """最小的假 surface —— 只记录 `sample` 被怎么调用的。

    **注意它没有 `report`**：B1 不声明任何判定，而「不给它那条路」比「记得
    别调」牢靠（模块 docstring 第六节）。`TestRun::test_it_declares_nothing`
    就是踩在这一点上的。
    """

    def __init__(self, prefixes: list[str], budget: int | None, seed: int = 0) -> None:
        self.prefixes = tuple(prefixes)
        self._budget = budget
        self._seed = seed
        self.calls: list[tuple[str, int]] = []

    @property
    def budget_left(self) -> int | None:
        return self._budget

    def sample(self, prefix: str, n: int) -> list[object]:
        self.calls.append((prefix, n))
        return [object()] * n

    def rng(self) -> random.Random:
        return random.Random(self._seed)


def _prefixes(n: int) -> list[str]:
    return [f"2001:db8:5:{i:x}::/64" for i in range(n)]


# ── 分配 ────────────────────────────────────────────────────────────────────


class TestPlanCounts:
    """分配本身：总量、均匀性、确定性，以及**它只用 `random()`**。"""

    BUDGET = 300
    N = 32

    def test_the_total_is_exactly_the_budget(self) -> None:
        """少一次是「没花完」，多一次会被 runner 拒（`BudgetExhausted`）。

        两种都不会被静默吞掉，但都会让这一趟的数与别的行不可比——所以钉死。
        """
        counts = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(0))
        assert sum(counts) == self.BUDGET
        assert len(counts) == self.N

    def test_the_same_seed_gives_the_same_allocation(self) -> None:
        """**T6a 的核心断言**：可复现随机源的意思是「同 seed ⇒ 同一条序列」。

        写成两个独立的 `Random` 而不是同一个对象调两次——后者测的是
        「对象记得上次的状态」，前者才是「种子决定一切」。
        """
        a = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(7))
        b = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(7))
        assert a == b, "同一个 seed 给出了两条不同的分配——「可复现」不成立"

    def test_different_seeds_give_different_allocations(self) -> None:
        """反方向。没有这一条，一个**无视种子**的实现（比如恒等分配）也能过上面那条。

        32 个前缀 / 300 次抽样下两次抽到同一条分配的概率按多项分布算远小于
        1e-30，所以这不是一条会偶发变红的测试。
        """
        a = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(0))
        b = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(1))
        assert a != b

    def test_it_only_touches_random(self) -> None:
        """`randrange` / `shuffle` / `sample` / `randint` 一个都不许用。

        理由不是风格：Python 只保证 `random()` 那条序列跨版本不变，用别的会让
        「同 seed 重跑逐包相同」在升一次 Python 之后**悄悄失效**。见 `_OnlyRandom`。
        """
        proxy = _OnlyRandom(random.Random(3))
        counts = plan_counts(tuple(_prefixes(self.N)), self.BUDGET, proxy)  # type: ignore[arg-type]
        assert sum(counts) == self.BUDGET
        assert proxy.calls == self.BUDGET, "抽样次数与预算不符——每包应当恰好一次"

    def test_the_allocation_is_uniform_in_expectation(self) -> None:
        """`E[每个前缀被抽中的次数] = budget / n`。

        用 200 个 seed 的均值去逼近。标准差按多项分布算：
        `sqrt(Var/n_seeds)`，`Var = budget·(1/n)·(1-1/n) ≈ 9.1`，
        于是均值的标准差 ≈ 0.21——取 `4σ ≈ 0.85` 为界，偶发变红的概率极低。
        """
        seeds = 200
        totals = [0] * self.N
        for seed in range(seeds):
            for i, count in enumerate(
                plan_counts(tuple(_prefixes(self.N)), self.BUDGET, random.Random(seed))
            ):
                totals[i] += count
        means = [total / seeds for total in totals]
        expected = self.BUDGET / self.N
        assert all(abs(m - expected) < 0.85 for m in means), (
            f"各前缀的均值 {[round(m, 2) for m in means]} 偏离 {expected}——"
            "分配不是均匀的，而「均匀」是 B1 的全部定义"
        )

    def test_a_small_budget_does_skip_prefixes(self) -> None:
        """预算与前缀数相当时，「某个前缀一次没被抽到」是**常态**。

        `P(某前缀被跳过) = (1 - 1/n)^budget`，n=32/预算=32 时约 **36%**。
        这条断言存在的理由是它**与冒烟表的规模相反**（见下一条）：先把
        「跳过」这条路走通，下一条才能诚实地说明它在 300 探下几乎不发生。
        """
        skipped_somewhere = False
        for seed in range(50):
            counts = plan_counts(tuple(_prefixes(32)), 32, random.Random(seed))
            if 0 in counts:
                skipped_somewhere = True
                break
        assert skipped_somewhere, (
            "50 个 seed 里一次都没出现「某前缀被跳过」——跳过这条路没被测到，"
            "而 run() 里那条 `if count == 0: continue` 正是为它写的"
        )

    def test_the_smoke_scale_almost_never_skips_a_prefix(self) -> None:
        """**冒烟表的规模下，B1 与 B0 的覆盖几乎分不开**——把算术钉在这里。

        `b0_probe_all` 的 docstring 说「B1 会给某个前缀 0 次、另一个 20 次」，
        据此说两者会在 `aliased_caught` 上因抽样噪声分开。**那个论断在 300 探下
        不成立**（模块 docstring 第五节）：`(31/32)^300 ≈ 7.3e-5`，32 个前缀
        合起来约 0.23%。这条断言把这个量级固定住，于是「B1 的方差比 B0 大」
        这类结论必须回来看一眼预算够不够。
        """
        n, budget = self.N, self.BUDGET
        p_skip = (1 - 1 / n) ** budget
        assert p_skip < 1e-3, (
            f"300 探下每个前缀被跳过的概率是 {p_skip:.2e}，比 1e-3 还大——"
            "那就不是「几乎不发生」，冒烟表的读法要改"
        )
        # 反方向：预算降到 100，跳过就不再罕见。没有这一条，上面那个数是
        # 「随便取个很小的数就绿」。
        assert n * (1 - 1 / n) ** 100 > 0.1

    def test_an_empty_prefix_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="没有前缀"):
            plan_counts((), self.BUDGET, random.Random(0))

    def test_a_non_positive_budget_is_refused(self) -> None:
        with pytest.raises(ValueError, match="预算至少为 1"):
            plan_counts(tuple(_prefixes(self.N)), 0, random.Random(0))


# ── `run()` ─────────────────────────────────────────────────────────────────


class TestRun:
    """`run()` 与 `plan_counts()` 之间那一层：跳过、总量、以及「不声明」。"""

    BUDGET = 300
    N = 32

    def test_zero_counts_are_not_sent(self) -> None:
        """`sample(prefix, 0)` 会被 runner 拒（`runner.py:177`），所以必须跳过。

        用一个会**跳过某些前缀**的小预算来测——大预算下这条分支根本走不到，
        而走不到的分支等于没测。
        """
        surface = FakeSurface(_prefixes(32), 32, seed=5)
        run(surface)  # type: ignore[arg-type]
        assert surface.calls, "一次都没发——预算 32 / 32 个前缀不该全跳过"
        assert all(n >= 1 for _, n in surface.calls), (
            f"发出去了 0 次的请求 {[c for c in surface.calls if c[1] < 1]}——"
            "runner 会拒绝它，现场是跑到一半的 SurfaceError"
        )

    def test_the_budget_is_spent_exactly(self) -> None:
        surface = FakeSurface(_prefixes(self.N), self.BUDGET, seed=11)
        run(surface)  # type: ignore[arg-type]
        assert sum(n for _, n in surface.calls) == self.BUDGET

    def test_each_prefix_is_sampled_at_most_once(self) -> None:
        """按前缀顺序**逐前缀一批**，不是逐包。同 B0 的理由：`v6 (d)` 明令预算
        按 epoch 粒度预留，逐包往返会把并发池重新串行化（`:397`）。"""
        surface = FakeSurface(_prefixes(self.N), self.BUDGET, seed=2)
        run(surface)  # type: ignore[arg-type]
        assert len({prefix for prefix, _ in surface.calls}) == len(surface.calls)

    def test_the_requests_follow_the_prefix_order(self) -> None:
        """请求顺序只取决于计数，不取决于抽签先后——两个随机源里少一个要复现的。"""
        surface = FakeSurface(_prefixes(self.N), self.BUDGET, seed=2)
        run(surface)  # type: ignore[arg-type]
        order = [prefix for prefix, _ in surface.calls]
        assert order == [p for p in surface.prefixes if p in set(order)]

    def test_it_declares_nothing(self) -> None:
        """B1 不声明任何判定——`aliased_caught` 上的 0 是**结构性 0**（第六节）。

        两条一起：**跑的时候够不到那条路**（假 surface 没有 `report`，而
        `run()` 跑完了），以及**源码里根本没有那个动词**。第二条比第一条强：
        它拦得住「`getattr(surface, "report", None)` 这类防御性写法」。
        """
        surface = FakeSurface(_prefixes(self.N), self.BUDGET, seed=3)
        run(surface)  # type: ignore[arg-type]
        source = (REPO_ROOT / "src" / "baselines" / "b1_uniform_random.py").read_text(
            encoding="utf-8"
        )
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "surface.report" not in code
        assert not hasattr(surface, "report")

    def test_a_missing_budget_raises(self) -> None:
        """握手没给预算时必须炸，**不猜默认值**。"""
        surface = FakeSurface(_prefixes(self.N), None)
        with pytest.raises(ValueError, match="budget_left 是 None"):
            run(surface)  # type: ignore[arg-type]


# ── 真的跑一遍（T6a 的种子通道验收）─────────────────────────────────────────


def _config_prefixes() -> list[str]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    return [
        str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        for entry in document["subnets"]
    ]


def _prefix_of(addr: str, prefixes) -> str | None:
    ip = ipaddress.ip_address(addr)
    best: ipaddress.IPv6Network | None = None
    for raw in prefixes:
        net = ipaddress.ip_network(raw, strict=False)
        if ip in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best) if best is not None else None


class _CountingExecutor:
    """不出网卡；按真值给回包，并把**逐前缀的探测数**记下来。

    `per_prefix()` 是这条通道的验收口：同 seed 跑两遍，它必须逐键相同。
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


class TestB1ThroughTheRealRunner:
    """**T6a 的验收：B1 在真子进程里跑得起来，而且同 seed 逐前缀相同。**

    跑的是完整那条路：真子进程 → 握手（**种子从这里进去**，协议 v2）→ 真
    `SurfaceServer` + 权威账本 → 真 config 当真值 → 真 `score()`。

    `driver.py` 的「今天能跑什么」表里，B1 那一栏此前写着「跑不了，缺一个
    可复现的随机源进子进程的通道」。**这组测试就是那一栏变成「能跑」的凭据。**
    """

    BUDGET = 300
    PREFIXES = 32
    ALIASED = 8

    def _session(self, seed: int):
        truth = stage1_truth_from_config(CONFIG_PATH)
        prefixes = _config_prefixes()
        executor = _CountingExecutor(truth)
        server = SurfaceServer(
            executor=executor,
            oracle=Oracle(truth_source=lambda: truth, reset_target=lambda seed: None),
            ledger=BudgetLedger(self.BUDGET),
            sampler=lambda prefix, n: [
                f"{prefix.split('/')[0].rstrip(':')}::{i + 1:x}" for i in range(n)
            ],
            prefixes=prefixes,
            seed=seed,
            epoch_size=20,
        )
        process = spawn_strategy(ENTRY, server=server, cwd=str(REPO_ROOT))
        return server, executor, truth, prefixes, process

    def test_the_session_runs_clean_and_spends_the_whole_budget(self) -> None:
        server, executor, _, _, process = self._session(seed=0)
        assert process.wait(timeout=120) == 0, process.stderr()
        assert server.ledger.left == 0, "预算没花完——B1 应该恰好花光"
        assert len(executor.responses) == self.BUDGET

    def test_the_same_seed_gives_the_same_per_prefix_counts(self) -> None:
        """**这条就是 T6a 的全部内容**：握手把种子送进去 ⇒ 随机源可复现。

        比较的是**逐前缀的探测数**，不是退出码也不是总分——总分在 300 探的
        规模上对 2.5% 量级的抖动不敏感（模块 docstring 第五节），拿它当判据
        会是一条**看起来在测可复现、其实测不出可复现**的测试。
        """
        _, first, _, prefixes, process = self._session(seed=13)
        assert process.wait(timeout=120) == 0, process.stderr()
        _, second, _, prefixes_again, process = self._session(seed=13)
        assert process.wait(timeout=120) == 0, process.stderr()

        a = first.per_prefix(prefixes)
        b = second.per_prefix(prefixes_again)
        assert a == b, (
            "同一个 seed 跑出两条不同的覆盖——种子没进到随机源里。"
            f"不同的前缀：{[p for p in a if a[p] != b[p]][:5]}"
        )
        assert len(set(a.values())) > 1, (
            "所有前缀拿到了一样多次——这看起来像 B0，不像均匀随机。"
            "（也可能是分配退化成常量，那样上面那条会永远通过。）"
        )

    def test_a_different_seed_gives_a_different_coverage(self) -> None:
        """反方向：**无视种子的实现**（比如恒等分配）能过上面那条，过不了这条。"""
        _, first, _, prefixes, process = self._session(seed=13)
        assert process.wait(timeout=120) == 0, process.stderr()
        _, second, _, prefixes_again, process = self._session(seed=14)
        assert process.wait(timeout=120) == 0, process.stderr()
        assert first.per_prefix(prefixes) != second.per_prefix(prefixes_again)

    def test_the_waste_ratio_lands_within_the_predicted_spread(self) -> None:
        """**预言 0.25 ± 4σ，实测应当落进去。**

        B0 的同一个数（`test_b0.py`）是构造上**精确**的 0.25，而 B1 的是随机的：
        `p = 8/32`、`n = 300`，标准差 `sqrt(p(1-p)/n) ≈ 2.5%`，所以界取 `0.25 ± 0.10`。
        **这条测试的用途是标定 B1 的噪声地板**，而那个数正是模块 docstring 第五节
        说「B1 与 B0 在冒烟规模上分不开」的依据。
        """
        _, executor, truth, prefixes, process = self._session(seed=4)
        assert process.wait(timeout=120) == 0, process.stderr()
        run_record = RunRecord(probes=tuple(executor.responses), reports=())
        result = score(run_record, truth)

        assert result.probes_charged == self.BUDGET
        assert result.aliased_caught == 0, "B1 不该声明任何判定（结构性 0）"
        assert abs(result.aliased_waste_ratio - 0.25) <= 0.10, (
            f"实测 {result.aliased_waste_ratio:.4f} 落在 0.25 ± 0.10 之外——"
            "要么分配不是均匀的，要么归属规则把包算错了前缀"
        )
