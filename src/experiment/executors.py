"""探测出口的两种形态 —— 编排层与「谁真的发包」之间的那一层。

数据流图 `:1116` 里 `executor` 是 runner 侧的第一个方框。它今天是二选一：

| | 是什么 | 什么时候用 |
|---|---|---|
| 真靶场 | `RawIcmp6Iface` + `ProbePool`，真网卡 | **测量**。要 Linux + `CAP_NET_RAW` |
| `OfflineExecutor` | 不出网卡，按「哪些前缀是 aliased」回包 | **排练**。开发机上把管线跑通 |

两者都只满足 `ProbeExecutor`（`runner.py:54`）。本模块加的那一样东西是
**`AccumulatingExecutor`**——它把「整趟的 `Response`」补上。

## 为什么要攒

`ProbeExecutor.run(targets) -> ProbeRun` 是**逐批**的：`SurfaceServer` 每收到
一条 `probe` / `sample` 就调一次，拿那一批的 `ProbeRun` 就走，**不保存**。
而 `score()` 要的是整趟的序列（`RunRecord.probes`）。测试里那份
`_RecordingExecutor`（`tests/unit/test_b0.py:205`）自己攒了一份——那是土办法，
一份一份地抄。这层把它变成接口的一部分，于是**任何** executor 都能被
`score()` 消费，编排层不必知道它是真的还是假的。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from typing import Protocol

from ..adapter.probe_pool import ProbeRun, ProbeTarget
from ..adapter.types import Response


class ProbeExecutor(Protocol):
    """与 `runner.ProbeExecutor` 同形 —— 这里重声明是为了不把它拖进来。"""

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun: ...


class AccumulatingExecutor:
    """给任何 `ProbeExecutor` 包一层，攒下整趟的 `Response`。

    它是**透明**的：`run()` 原样返回底层那份 `ProbeRun`，`SurfaceServer` 那边
    感觉不到区别（它要的 `responses` / `send_failed` 都在）。多出来的只有
    `probes()` —— 供装配 `RunRecord` 用。

    **每次会话一个新的。** 共用一个实例会让上一趟的包落进这一趟的分母里，
    而那正是「`aliased_waste_ratio` 的分母错了」那种静默失效。
    """

    def __init__(self, inner: ProbeExecutor) -> None:
        self._inner = inner
        self._responses: list[Response] = []
        #: 底层报的发送失败数，累计。
        self.send_failed = 0

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        run = self._inner.run(targets)
        self._responses.extend(run.responses)
        self.send_failed += run.send_failed
        return run

    def probes(self) -> tuple[Response, ...]:
        """整趟按序攒下的 `Response` —— `RunRecord.probes` 的来源。"""
        return tuple(self._responses)

    def __len__(self) -> int:
        return len(self._responses)


class OfflineExecutor:
    """不出网卡的靶场 —— **排练用，不是测量**。

    **它只判 `aliased`，不建模四档响应率。** 于是「有内容的前缀」在这张
    靶场里与「沉默的前缀」看起来一样（都不回包），而真实靶场不是那样：
    它有 `0.30` / `0.10` / `0.02` 这些中间档位，trap 故事的整条曲线就架在
    那上面。

    这不是缺陷，是**刻意的边界**，理由与 `tests/unit/test_b0.py:205` 的同名
    假件一样：「一个『照 config 速率掷骰子』的假靶场会把一份我们没验证过的
    仿真结论混进验收里」。四档速率的可达性有真实的坑（`ground_truth.py`
    第一节「一处已知的偏离」），照它写一遍等于**在 Python 里重新实现一遍
    上游 C++**，而那份实现没有任何东西对账。

    所以：

    * 它**能**支撑的：管线通不通、`report()` 通道、结构性 0、记账对账。
    * 它**不能**支撑的：任何关于「谁更会绕开 aliased」的结论。
      **由它得出的数不进入任何报告。**

    真实的那一份在 `src/experiment/real.py`（Linux），测量一律走它。
    """

    def __init__(self, aliased_prefixes: Iterable[str], prefixes: Sequence[str]) -> None:
        self._nets = [(ipaddress.ip_network(p, strict=False), p) for p in prefixes]
        #: 规范化后的 aliased 前缀集合 —— 回包的唯一依据。
        self.aliased = frozenset(
            str(ipaddress.ip_network(p, strict=False)) for p in aliased_prefixes
        )

    def _matched(self, dst: str) -> str | None:
        """**最长前缀匹配**，与 `score.py` 的归属规则同一条。

        不能用 top-level 的 `any(ip in net)`：config 的前缀可以嵌套（`/48`
        与 `/64`），那样一条 `/64` 的探测会被更宽的 `/48` 先认领，于是
        「哪条前缀回的包」在两处不一致。
        """
        ip = ipaddress.ip_address(dst.split("/")[0])
        best: ipaddress.IPv6Network | None = None
        best_raw: str | None = None
        for net, raw in self._nets:
            if ip in net and (best is None or net.prefixlen > best.prefixlen):
                best, best_raw = net, raw
        return best_raw

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        out: list[Response] = []
        for target in targets:
            dst = target.dst if isinstance(target, ProbeTarget) else str(target)
            raw = self._matched(dst)
            hit = raw is not None and str(ipaddress.ip_network(raw, strict=False)) in self.aliased
            out.append(
                Response(status="received", dst=dst, src=dst, hop_limit=64, rtt_ms=1.0)
                if hit
                else Response(status="sent_no_reply", dst=dst)
            )
        return ProbeRun(responses=tuple(out), sent=len(out))


__all__ = [
    "AccumulatingExecutor",
    "OfflineExecutor",
    "ProbeExecutor",
]
