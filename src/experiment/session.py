"""**一次会话** = 一个 `(策略, seed)` 的一次完整运行 —— 编排层(H1)。

这是缺失的那层胶水。T1–T5、T10、T13 交付的都是**部件**：适配层、真值层、
评分器、决策缓存、执行沙箱、配置。而「**把部件装成一次实验**」这件事，
此前唯一的落点是 `tests/unit/test_b0.py::TestB0ThroughTheRealRunner._session()`
——一个测试函数，用一个不出网卡的假 executor，只跑 B0 一行。

本模块把它抽出来、一般化，并补上测试版缺的那一块：**整趟的 `Response` 序列**。

## 一次会话的九步

```
  1. 读 config        → 真值(Stage1Truth) + 前缀表 + 预算
  2. 造 Oracle        → truth_source / reset_target，**只在 runner 侧**
  3. reset_target(seed)   ← 必须在子进程起飞**之前**
  4. 造 BudgetLedger  → 权威账本，`v6 (d)` 明令它必须在 runner 侧
  5. 造 SurfaceServer → 四个注入项：executor / oracle / ledger / sampler
  6. spawn_strategy   → 真子进程，只经 stdio 拿到 `ProbeSurface`
  7. 收工             → 子进程退出码 + 服务端收尾异常
  8. 装配 RunRecord   → `probes` 来自 executor 攒的，`reports` 来自 server
  9. score()          → 折分
```

第 8 步是**这一层存在的理由**：`ProbeExecutor.run()` 是逐批的，
`score()` 要的是整趟的。这个落差此前只能靠每个测试自己攒一份。

## 退出码不是零就**炸**，不返回一个能被折分的对象

一个非零退出的策略（越界被拒、预算耗尽没接住、自己崩了）**跑出来的数
看起来完全正常**——`score()` 不知道它截断了，只会照着收到的包算分母。
这与失败模式表反复点名的那种形状是同一条：**静默**。所以失败要以
`SessionFailed` 冒出来，而**不是**降级成「这次跑得不好」。

## 种子怎么进子进程（T6a 定夺，2026-10-10）

`reset_target(seed)` 与 pool 的 `identifier` 都从 seed 派生，**策略进程也拿到它**
——经 `SurfaceServer(seed=…)` → 握手回复的 `result["seed"]` →
`ProbeSurface(seed=…)`。B1(均匀随机) / B4(bandit) / M1 / M2 都需要一个可复现的
随机源，而这是它们拿到种子的**唯一**一条路。

这里此前写的是「种子今天不进子进程，因为那条通道是 T6a 尚未定夺的接口决定」。
**T6a 已定夺：走握手，不走环境变量。** 三条理由（会拒绝 vs 会漏设、会话参数
只有一条路、在 ① 条边上可审计）写在 `surface.py` 模块 docstring 的
「种子从哪来」一节，代价写在 `protocol.TRUTH_KEYS` 旁边。协议版本因此从 1 升到
**2**——一个 v1 的 runner 不会发种子，也会被 `_hello` 当场拒绝。

**注意种子的两个用途在 runner 侧是分开的**：`reset_target(seed)` 重置的是
**靶场**（Stage 1 是空操作，靶场没有由 seed 决定的状态——见 `driver._build_spec`），
而交给策略的那一份是**策略自己的**随机源。Stage 2 的 `RouterShim` 两者都要用，
而「策略知道了配对种子」这件事在 Stage 2 是一个**具名的**待办（种子空间只有 30
个数，任何派生子在枚举下可逆），归 T15。
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapter.ground_truth import stage1_truth_from_config
from ..adapter.oracle import Oracle
from ..adapter.runner import BudgetLedger, ProbeExecutor, SurfaceServer, spawn_strategy
from ..adapter.truth import Stage1Truth, Stage2Truth
from ..scoring.score import RunRecord, Score, score
from .executors import AccumulatingExecutor

#: `(prefix, n) -> 地址序列` —— 与 `SurfaceServer.sampler` 同一个签名。
Sampler = Callable[[str, int], Sequence[str]]


class SessionFailed(RuntimeError):
    """这一趟**不是一次有效的测量**，所以没有数可以折。

    **它不得降级成「这次跑得不好」。** 一个非零退出的策略（越界被拒、
    预算耗尽没接住、自己崩了）截断了探测，而 `score()` 只看收到的包——
    于是它会照着一个残局算出 `aliased_waste_ratio` 这类比率，数看起来
    完全正常。要区分这两件事，只能靠**退出码，不是靠数**。
    """

    def __init__(self, entry: str, seed: int, exit_code: int, stderr: str) -> None:
        self.entry = entry
        self.seed = seed
        self.exit_code = exit_code
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-12:]
        super().__init__(
            f"策略 {entry} 在 seed {seed} 上以退出码 {exit_code} 结束——"
            "这一趟不是一次有效的测量，不折分。\n"
            "子进程 stderr 末 12 行：\n  " + "\n  ".join(tail)
        )


@dataclass(frozen=True, slots=True)
class SessionResult:
    """一次干净跑完的会话。构造它就意味着退出码是 0。"""

    strategy: str
    seed: int
    score: Score
    #: 权威账本上花掉的探测数（= `budget - ledger.left`）。
    charged: int
    #: `SurfaceServer.probes_run` —— 经过 runner 的目标地址总数。
    probes_run: int
    #: `report()` 收到过的判定，按序。`aliased_caught` 的唯一来源。
    reports: tuple[tuple[str, bool], ...]
    #: 这一趟的挂钟秒数。**门 1 条件 11 要的就是这个数**（「冒烟表有一次
    #: 实测总时长，且与预算上限对得上」），所以它由编排层测，不由脚本估。
    elapsed_s: float


def load_stage1(config_path: Path) -> tuple[Stage1Truth, tuple[str, ...], int]:
    """读 Stage 1 的 config：真值 + 前缀表 + 预算。

    前缀**按 config 的书写顺序**取出并规范化——runner 就是这样把它们交给
    策略的，而 `test_stage1_config.py` 已经断言 `read_subnets` 保持那个顺序。
    顺序是承重的：B0 的余数摊平与 `aliased_waste_ratio` 的 `0.25` 都建立在
    「aliased 是列表前 1/4」上。

    Raises:
        KeyError: config 里没有 `budget`。**不猜默认值**——猜一个预算要么
            花不完、要么被 runner 拒（`SurfaceError`），两种失败都表现为
            「跑完了，结果不对」。
    """
    truth = stage1_truth_from_config(config_path)
    with Path(config_path).open("r", encoding="utf-8") as handle:
        document: dict[str, Any] = json.load(handle)
    prefixes = tuple(
        str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        for entry in document["subnets"]
    )
    budget = int(document["budget"])
    return truth, prefixes, budget


def stride_sampler(prefix: str, n: int) -> list[str]:
    """Stage 1 的**确定性**采样：前缀里从第 1 个主机地址起连续取 `n` 个。

    **它不是均匀随机**，而均匀随机那一支是 B1/B3 的定义（B3 bit-reversal
    照 6Seeks Theorem 1，归 T6b）。编排层今天选确定性的，是因为采样规则一
    进来就是**第二个随机源**，而本层的第一个目标是把**可复现**跑通，不是把
    随机性做对。把「取哪些地址」变成策略能选的变量，是 `SurfaceServer.sampler`
    的设计意图（`runner.py` 模块 docstring）——但那是给策略找差异用的，
    不是编排层的必需品。

    跳过全零主机地址（anycast），与 `scripts/perf_calibrate.addresses` 同一条
    规避。
    """
    net = ipaddress.ip_network(prefix, strict=False)
    if n < 1:
        raise ValueError(f"n 至少为 1，得到 {n}")
    base = net.network_address
    addresses = [str(base + i + 1) for i in range(n)]
    for addr in addresses:
        if ipaddress.ip_address(addr) not in net:
            raise ValueError(
                f"{prefix} 里取不出 {n} 个地址（取到 {addr} 已越出前缀）——"
                "前缀太小，或者 n 太大"
            )
    return addresses


def run_session(
    *,
    entry: str,
    seed: int,
    truth: Stage1Truth | Stage2Truth,
    prefixes: Sequence[str],
    budget: int,
    sampler: Sampler,
    executor: AccumulatingExecutor,
    reset_target: Callable[[int], None],
    cwd: Path | None = None,
    timeout_s: float = 300.0,
) -> SessionResult:
    """跑一个 `(策略, seed)`，返回折好分的结果。

    Args:
        entry: 策略入口，形如 `"pkg.module:run"`。契约是 `run(surface) -> None`。
        seed: 配对单位 `(config, seed)` 里的那个 seed（`:559`）。它派生三样东西
            ——`reset_target(seed)`、（真 executor 的）`identifier`，以及**交给
            子进程的那一份随机源**（T6a：经 `SurfaceServer(seed=…)` 走握手，
            见模块 docstring）。
        truth: 已经导出的真值。**它不进子进程**：`spawn_strategy` 只把
            `SurfaceServer` 接在 stdio 上，而 server 只经
            `protocol.assert_no_truth_keys` 过的帧说话。
        executor: **已经**是 `AccumulatingExecutor`——第 8 步要读它的
            `.responses`。由调用方先包好，是为了让「谁攒的」只有一处。
        reset_target: 靶场重置。**必填**：缺省成空操作会让 A/B 对比悄悄
            失去可重复性（`:325`），而症状是「结果飘」，不是报错。
        timeout_s: 子进程的等待上限，**也给服务端报一个信息性口径**。

    Raises:
        SessionFailed: 子进程退出码非零。**不返回半个结果**——见模块
            docstring 与 `SessionFailed`。
    """
    oracle = Oracle(truth_source=lambda: truth, reset_target=reset_target)
    # 先重置靶场，再起飞子进程。反过来会让第一轮探测打在上一轮的状态上，
    # 而症状是「结果飘」——两次 run 的前几个包不一样。
    oracle.reset_target(seed)

    ledger = BudgetLedger(budget)
    server = SurfaceServer(
        executor=executor,
        oracle=oracle,
        ledger=ledger,
        sampler=sampler,
        prefixes=prefixes,
        seed=seed,
        timeout_s=timeout_s,
    )

    started = time.perf_counter()
    process = spawn_strategy(entry, server=server, cwd=str(cwd) if cwd is not None else None)
    try:
        exit_code = process.wait(timeout=timeout_s)
    finally:
        elapsed_s = time.perf_counter() - started

    if exit_code != 0:
        raise SessionFailed(entry, seed, exit_code, process.stderr())

    run = RunRecord(probes=executor.probes(), reports=tuple(server.reports))
    return SessionResult(
        strategy=entry,
        seed=seed,
        score=score(run, truth),
        charged=ledger.spent,
        probes_run=server.probes_run,
        reports=tuple(server.reports),
        elapsed_s=elapsed_s,
    )


__all__ = [
    "Sampler",
    "SessionFailed",
    "SessionResult",
    "load_stage1",
    "run_session",
    "stride_sampler",
]
