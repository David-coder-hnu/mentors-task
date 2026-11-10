"""**实验编排层** —— `策略 × seed` 的矩阵，一行一行跑，一行一行折分。

这一层在 T1–T19 里**没有编号**，而门 1 的通过条件（「冒烟表跑通」，
`ceo-plan.md:236`）与门 1 条件 2（「一键复现脚本」）都要求它存在。
它的前身是 CEO plan 的 **C6**（「一键复现脚本 + 决策缓存打包」），进了门 1
的工时估算却没有落成任务块。

## 为什么它必须单独存在

`tests/unit/test_b0.py::TestB0ThroughTheRealRunner` 已经证明这条路是通的，
但它是一**个测试**：一个策略（B0）、一个 seed、一个假 executor。冒烟表要的是
**5 行 × 30 seeds = 150 趟**（`:699`），每趟都要一个新的账本、一个新的
`SurfaceServer`、一个新的 executor——**共用一个就是上一趟的包落进这一趟的
分母**。

## 今天能跑什么，不能跑什么

| | 状态 |
|---|---|
| `B0`（全探一遍） | **能跑**。它不看观测，所以不需要随机源 |
| `B1`（均匀随机） | **能跑**（T6a） |
| `B4`（响应率自适应 bandit） | **能跑**（T6a） |
| `M1` / `M2`（记忆族） | **能跑**。M1 每趟冷启动；M2 读取并回写自己的 `pulls/rewards` 历史 |

最后一栏此前是「B1 / B4 / M1 / M2 跑不了，缺一个可复现的随机源进子进程的通道」
——那个缺口由 **T6a 定夺并落地**：种子走**握手**（`SurfaceServer(seed=…)` →
`reply.result["seed"]` → `ProbeSurface(seed=…)`），不走 `spawn_strategy` 的
`extra_env`。协议因此从 v1 升到 **v2**，所以一个 v1 的 runner 会被
`strategy_child._hello` 当场拒绝——那种运行**没有机会产生数字**，这正是版本号
存在的意义。三条理由写在 `surface.py` 模块 docstring 的「种子从哪来」一节。

`M1`/`M2` 由协议 v3 的 `epoch_size` 与 `memory` 驱动：两行共享同一 UCB1
底座，M1 忽略历史，M2 只读取本策略自己的 `pulls/rewards` 快照并在收尾回写。

## 「一键」到什么程度

```
cd <repo> && .venv/Scripts/python.exe -m src.experiment --offline --seeds 0-2
```

一条命令、零参数（`--config` 默认 `config/stage1_config.json`，`--strategy`
默认 B0）。`docs/artifacts` 里 CEO plan 的原话是「**复现脚本必须一条命令跑完**，
否则 oracle 的价值减半」（`:788`）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapter.truth import Stage1Truth, Stage2Truth
from ..adapter.runner import DecisionProvider
from ..agent.cache import DecisionCache
from ..adapter.memory import MemorySnapshot
from ..scoring.score import Score
from .executors import AccumulatingExecutor, OfflineExecutor, ProbeExecutor
from .session import (
    Sampler,
    SessionResult,
    load_stage1,
    load_stage1_context,
    run_session,
    stride_sampler,
)
from .stage2 import Stage2Context, load_stage2_context
from .decision_broker import DecisionBroker, command_completion

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "stage1_config.json"

#: 冒烟表的五行，**写死在这里**（`:628`）。`B0` 是退化警报、`B1` 是下界、
#: `B4` 是记忆族的无记忆控制组、`M1`/`M2` 是记忆族两行。
#: **A1/A2 不在 Stage 1 参赛**（`:628`），`B2/B3/B5` 属 Stage 2（`:633`）。
SMOKE_TABLE_ROWS: tuple[str, ...] = (
    "src.baselines.b0_probe_all:run",
    "src.baselines.b1_uniform_random:run",
    "src.baselines.b4_response_bandit:run",
    "src.baselines.m1_memory_cold:run",
    "src.baselines.m2_memory_warm:run",
)

#: Stage 2 的静态基线；A1/A2 另列，因为它们需要 runner 侧决策代理。
STAGE2_BASELINE_ROWS: tuple[str, ...] = (
    "src.baselines.b0_probe_all:run",
    "src.baselines.b1_uniform_random:run",
    "src.baselines.b2_response_density:run",
    "src.baselines.b3_bit_reversal:run",
    "src.baselines.b4_response_bandit:run",
    "src.baselines.b5_frp_aware:run",
)
STAGE2_AGENT_ROWS: tuple[str, ...] = (
    "src.agent.strategies:run_a1",
    "src.agent.strategies:run_a2",
    "src.agent.strategies:run_a3",
)

#: 今天真正跑得起来的那一行。默认值取它而不是整个 `SMOKE_TABLE_ROWS`，
#: 是因为「一条命令跑完」在缺四条策略时会红——**红是对的，但不该是默认**。
DEFAULT_ROW = SMOKE_TABLE_ROWS[0]


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """一次实验的**全部**输入 —— 少一样都不跑。

    `executor_factory` 收 seed：真靶场那一支要拿它派生 `identifier`
    （`probe_pool.py:18`）。排练那一支忽略它，但**签名一样**——否则
    「换 executor 就得改编排层」多出一条不该有的接缝。
    """

    config_path: Path
    executor_factory: Callable[[int], ProbeExecutor]
    reset_target: Callable[[int], None]
    sampler: Sampler = stride_sampler
    cwd: Path | None = REPO_ROOT
    timeout_s: float = 300.0
    #: `None` = 从 config 读。给了就是**覆盖**，给测试用。
    budget: int | None = None
    stage: int = 1
    decision_provider_factory: Callable[[int], DecisionProvider] | None = None


@dataclass(frozen=True, slots=True)
class RowResult:
    """矩阵里的一格。"""

    strategy: str
    seed: int
    session: SessionResult

    @property
    def score(self) -> Score:
        return self.session.score


@dataclass(frozen=True, slots=True)
class MatrixResult:
    """整张表。"""

    rows: tuple[RowResult, ...]
    #: 整张表的挂钟秒数。**门 1 条件 11 要的就是这个数**：原文是「冒烟表有
    #: 一次实测总时长，且与预算上限对得上」——所以它由编排层测，不由脚本估。
    elapsed_s: float

    def by_strategy(self) -> dict[str, tuple[RowResult, ...]]:
        out: dict[str, list[RowResult]] = {}
        for row in self.rows:
            out.setdefault(row.strategy, []).append(row)
        return {name: tuple(group) for name, group in out.items()}

    def records(self) -> list[dict[str, Any]]:
        """机器可读的逐行记录 —— `Score` 的每一个字段都带上。

        **不挑字段**：挑出来的表会在端点定义改了之后**静默地少一列**，
        而少的那一列恰恰可能是这次改动要看的那一列（T14 要动的正是端点）。
        """
        out: list[dict[str, Any]] = []
        for row in self.rows:
            record: dict[str, Any] = {"strategy": row.strategy, "seed": row.seed}
            record.update(dataclasses.asdict(row.score))
            record["charged"] = row.session.charged
            record["probes_run"] = row.session.probes_run
            record["reports"] = [list(item) for item in row.session.reports]
            record["elapsed_s"] = row.session.elapsed_s
            record["memory_prefixes"] = sum(
                1 for entry in row.session.memory.values() if entry["pulls"] > 0
            )
            record["memory_pulls"] = sum(entry["pulls"] for entry in row.session.memory.values())
            if row.session.stage2 is not None:
                record["stage2"] = dataclasses.asdict(row.session.stage2)
            record["decision_calls"] = row.session.decision_calls
            record["decision_cache_hits"] = row.session.decision_cache_hits
            record["decision_elapsed_s"] = list(row.session.decision_elapsed_s)
            record["decision_records"] = list(row.session.decision_records)
            out.append(record)
        return out


def run_row(
    spec: ExperimentSpec,
    strategy: str,
    seed: int,
    *,
    truth: Stage1Truth | Stage2Truth,
    prefixes: Sequence[str],
    budget: int,
    epoch_size: int,
    memory: Mapping[str, object] | None = None,
) -> RowResult:
    """跑矩阵里的一格。

    **真值与前缀由调用方传进来**，不在这里读 config：150 趟各读一遍
    config 不算贵，但它会让「所有行用的是同一份真值」变成一句要读代码
    才能确认的话。`run_matrix` 读一次、传下去。
    """
    executor = AccumulatingExecutor(spec.executor_factory(seed))
    session = run_session(
        entry=strategy,
        seed=seed,
        truth=truth,
        prefixes=prefixes,
        budget=budget,
        epoch_size=epoch_size,
        memory=memory,
        decision_provider=(
            spec.decision_provider_factory(seed)
            if spec.decision_provider_factory is not None
            else None
        ),
        sampler=spec.sampler,
        executor=executor,
        reset_target=spec.reset_target,
        cwd=spec.cwd,
        timeout_s=spec.timeout_s,
    )
    return RowResult(strategy=strategy, seed=seed, session=session)


def run_matrix(
    spec: ExperimentSpec,
    strategies: Sequence[str],
    seeds: Sequence[int],
) -> MatrixResult:
    """把 `strategies × seeds` 全跑一遍。

    **外层是策略、内层是 seed**（不是反过来）：同一策略的 30 趟连在一起，
    中途炸掉时已经跑完的那些策略是完整的——配对检验以 `(config, seed)`
    为单位（`:559`），半个策略的行进不了任何检验。

    Raises:
        SessionFailed: 任何一趟退出码非零。**立刻炸，不跑完剩下的**——
            「跑完再说」会让一张缺格子的表看起来像一张完整的表。
    """
    if not strategies:
        raise ValueError("没有策略就没有矩阵")
    if not seeds:
        raise ValueError("没有 seed 就没有配对单位")

    if spec.stage == 2:
        context2 = load_stage2_context(spec.config_path)
        budget = context2.budget if spec.budget is None else spec.budget
        truth = context2.truth
        prefixes = context2.prefixes
        epoch_size = context2.epoch_size
        executor_factory = lambda seed: context2.executor(seed)
        reset_target = lambda seed: None
    elif spec.stage == 1:
        context = load_stage1_context(spec.config_path)
        budget = context.budget if spec.budget is None else spec.budget
        truth = context.truth
        prefixes = context.prefixes
        epoch_size = context.epoch_size
        executor_factory = spec.executor_factory
        reset_target = spec.reset_target
    else:
        raise ValueError(f"stage 只能是 1 或 2，得到 {spec.stage}")
    memory_by_strategy: dict[str, MemorySnapshot] = {}

    started = time.perf_counter()
    rows: list[RowResult] = []
    for strategy in strategies:
        for seed in seeds:
            row_spec = dataclasses.replace(
                spec,
                executor_factory=executor_factory,
                reset_target=reset_target,
            )
            row = run_row(
                row_spec,
                strategy,
                seed,
                truth=truth,
                prefixes=prefixes,
                budget=budget,
                epoch_size=epoch_size,
                memory=memory_by_strategy.get(strategy),
            )
            rows.append(row)
            memory_by_strategy[strategy] = row.session.memory
    return MatrixResult(rows=tuple(rows), elapsed_s=time.perf_counter() - started)


# ── 命令行 ──────────────────────────────────────────────────────────────────


def _parse_seeds(raw: str) -> tuple[int, ...]:
    """`"0-29"` / `"0,1,2"` / `"7"` → seed 元组。**顺序保持输入顺序。**

    负 seed 在这里就拒掉，不等到 `identifier_for_seed`（`real.py`）那一步：
    `--seeds -1` 走到那儿会报一个关于 ICMPv6 identifier 的错，而真正的问题
    是「你要了一个不存在的 seed」。**报错要指向原因，不是指向现场。**
    """
    seeds: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        low_raw, sep, high_raw = chunk.partition("-")
        if sep and low_raw.isdigit() and high_raw.isdigit():
            low, high = int(low_raw), int(high_raw)
            if high < low:
                raise ValueError(f"seed 区间 {chunk!r} 是倒的")
            seeds.extend(range(low, high + 1))
            continue
        try:
            value = int(chunk)
        except ValueError as exc:
            raise ValueError(f"seed 不是整数也不是区间：{chunk!r}") from exc
        if value < 0:
            raise ValueError(f"seed 必须非负：{value}")
        seeds.append(value)

    if not seeds:
        raise ValueError(f"没解析出任何 seed：{raw!r}")
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"seed 有重复：{raw!r}——同一趟跑两遍会重复计进矩阵")
    return tuple(seeds)


def _executor_source(args: argparse.Namespace) -> tuple[Callable[[int], ProbeExecutor], str]:
    """`(executor 工厂, 给 stderr 的一行说明)`。

    真靶场那一支的 import 放在函数里（惰性）：`src.adapter.iface` 是
    Linux-only，模块顶层导入它会让整个 `src.experiment` 在开发机上导不进来，
    **于是单测也跑不了**——而单测是这条路上唯一的自动化验证。见 `real.py`。
    """
    if args.stage == 2:
        context = load_stage2_context(args.config)
        return context.executor, "RouterShim（Response 层合成间接响应）"

    if args.offline:
        truth, prefixes, _ = load_stage1(args.config)
        # 排练那一支的回包判据取自**读出来的真值**，所以它拿不到 config 就造不出来。
        aliased = [p for p, fields in truth.prefixes.items() if fields.get("aliased")]

        def factory(_seed: int) -> ProbeExecutor:
            return OfflineExecutor(aliased, prefixes)

        return factory, "offline（排练，不出网卡）"

    from .real import probe_executor

    probe_timeout = args.probe_timeout
    if probe_timeout is None:
        with Path(args.config).open("r", encoding="utf-8") as handle:
            probe_timeout = float(json.load(handle)["W_s"])
    if probe_timeout <= 0:
        raise ValueError(f"--probe-timeout 必须为正数，得到 {probe_timeout}")

    def real_factory(seed: int) -> ProbeExecutor:
        return probe_executor(
            seed,
            max_in_flight=args.max_in_flight,
            timeout_s=probe_timeout,
        )

    return real_factory, (
        f"真靶场（max_in_flight={args.max_in_flight}, probe_timeout={probe_timeout:g}s）"
    )


def _build_spec(args: argparse.Namespace) -> ExperimentSpec:
    factory, source = _executor_source(args)
    sys.stderr.write(f"[experiment] 探测出口：{source}\n")
    return ExperimentSpec(
        config_path=args.config,
        executor_factory=factory,
        # Stage 1 的靶场**没有**由 seed 决定的状态：回包判据是
        # `hash(dst) % 1000`（`:736`），与 seed 无关。所以这里是空操作——
        # **不是**「忘了接重置」。Stage 2 的 shim 才有 seed 相关的状态。
        reset_target=lambda seed: None,
        cwd=REPO_ROOT,
        timeout_s=args.timeout,
        stage=args.stage,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.experiment",
        description="跑一张 策略 × seed 的矩阵，并把每一格折成分。",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--strategy",
        action="append",
        default=None,
        help=f"策略入口，可重复。默认 {DEFAULT_ROW}",
    )
    parser.add_argument("--seeds", default="0", help='"0-29" 或 "0,1,2"，默认 "0"')
    parser.add_argument("--out", type=Path, default=None, help="把逐行记录写进这个 JSON")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="用排练靶场（不出网卡，只判 aliased）。**由它得出的数不进入报告**",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=256,
        help="真靶场的在途探测数；必须 ≤ 本环境标定的无丢包上界（默认 256）",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=None,
        help="单批 sample 的排空上限；默认读 config 的 W_s",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="每趟的挂钟上限（秒）")
    parser.add_argument(
        "--stage",
        type=int,
        choices=(1, 2),
        default=1,
        help="1=Stage 1 Punching Bag；2=RouterShim 合成间接响应",
    )
    parser.add_argument(
        "--decision-cache",
        type=Path,
        default=None,
        help="A1/A2 决策缓存 JSONL；未命中时按 completion 配置决定是否联网",
    )
    parser.add_argument(
        "--completion-command",
        default=None,
        help="A1/A2 的本地 completion 命令；prompt 走 stdin，原始动作走 stdout",
    )
    parser.add_argument(
        "--completion-timeout",
        type=float,
        default=120.0,
        help="单次 completion 命令的挂钟上限（秒）",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="覆盖 config 每趟探测预算（Agent 冒烟可先设小，不用于主表）",
    )
    args = parser.parse_args(argv)

    args.config = Path(args.config)
    if args.stage == 2 and args.config == DEFAULT_CONFIG:
        args.config = REPO_ROOT / "config" / "stage2_config.json"
    strategies = tuple(args.strategy) if args.strategy else (DEFAULT_ROW,)
    seeds = _parse_seeds(args.seeds)
    spec = _build_spec(args)
    if args.budget is not None:
        spec = dataclasses.replace(spec, budget=args.budget)
    agent_rows = any("src.agent.strategies:" in strategy for strategy in strategies)
    cache: DecisionCache | None = None
    if agent_rows:
        if args.stage != 2:
            raise ValueError("A1/A2 只在 Stage 2 参赛；加 --stage 2")
        context = load_stage2_context(args.config)
        cache = (
            DecisionCache.load(args.decision_cache)
            if args.decision_cache is not None
            else DecisionCache()
        )
        completion = (
            command_completion(
                shlex.split(args.completion_command),
                timeout_s=args.completion_timeout,
            )
            if args.completion_command
            else None
        )
        spec = dataclasses.replace(
            spec,
            decision_provider_factory=lambda seed: DecisionBroker(
                config=context.config,
                seed=seed,
                cache=cache,
                completion=completion,
                protected_paths=(args.decision_cache,) if args.decision_cache else (),
            ),
        )

    try:
        result = run_matrix(spec, strategies, seeds)
    except BaseException:
        if cache is not None and args.decision_cache is not None:
            cache.save(args.decision_cache)
        raise

    stage2_rows = any(row.session.stage2 is not None for row in result.rows)
    header = (
        f"{'strategy':<38} {'seed':>4} {'waste':>8} {'caught':>7} "
        f"{'routers':>7} {'H(router)':>9} {'charged':>8} {'probes':>7} {'sec':>7}"
        if stage2_rows
        else f"{'strategy':<38} {'seed':>4} {'waste':>8} {'caught':>7} {'charged':>8} {'probes':>7} {'sec':>7}"
    )
    print(header)
    print("-" * len(header))
    for row in result.rows:
        score_ = row.score
        prefix = (
            f"{row.strategy:<38} {row.seed:>4} "
            f"{score_.aliased_waste_ratio:>8.4f} {score_.aliased_caught:>7} "
        )
        if stage2_rows:
            stage2 = row.session.stage2
            assert stage2 is not None
            prefix += f"{stage2.true_routers:>7} {stage2.router_entropy_bits:>9.3f} "
        print(
            prefix
            + f"{row.session.charged:>8} {row.session.probes_run:>7} "
            + f"{row.session.elapsed_s:>7.2f}"
        )
    print("-" * len(header))
    print(f"rows={len(result.rows)} wall_clock_s={result.elapsed_s:.2f}")

    if args.out is not None:
        if not agent_rows:
            agent_provenance = "not-applicable"
        elif args.completion_command is None:
            agent_provenance = "cache-replay"
        elif "deterministic_agent_completion" in args.completion_command:
            agent_provenance = "fixture"
        else:
            agent_provenance = "completion-command"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with Path(args.out).open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "config": str(args.config),
                    "strategies": list(strategies),
                    "seeds": list(seeds),
                    "budget_override": args.budget,
                    "completion_command": args.completion_command,
                    "decision_cache": (
                        str(args.decision_cache) if args.decision_cache is not None else None
                    ),
                    "agent_provenance": agent_provenance,
                    "wall_clock_s": result.elapsed_s,
                    "rows": result.records(),
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        sys.stderr.write(f"[experiment] 逐行记录写到 {args.out}\n")
    if cache is not None and args.decision_cache is not None:
        cache.save(args.decision_cache)
        stats = cache.stats
        sys.stderr.write(
            f"[experiment] 决策缓存写到 {args.decision_cache}"
            f"（累计命中 {stats.hits}，未命中 {stats.misses}）\n"
        )

    return 0


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_ROW",
    "SMOKE_TABLE_ROWS",
    "STAGE2_AGENT_ROWS",
    "STAGE2_BASELINE_ROWS",
    "ExperimentSpec",
    "MatrixResult",
    "RowResult",
    "main",
    "run_matrix",
    "run_row",
]
