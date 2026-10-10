"""真靶场的探测出口 —— `RawIcmp6Iface` + `ProbePool`。**这是测量那一支。**

`OfflineExecutor`（`executors.py`）是排练；**任何要进报告的数都出自这里**。
两者的差别不是精度，是**在不在真实的丢包/时序世界里**：排练那张靶场里
「有内容的前缀」与「沉默的前缀」看起来一样，而 trap 故事的整条曲线架在
真实靶场的中间档位上（`0.30` / `0.10` / `0.02`）。

## 为什么导入是惰性的

`src.adapter.iface` 是 Linux-only（`AF_INET6` + `SOCK_RAW` + `CAP_NET_RAW`）。
在开发机（Windows）上导入它、或在编排层的模块顶层导入，会让**整个
`src.experiment` 包在 Windows 上导不进来**——于是单测跑不了，而单测正是
这条路上唯一的自动化验证。所以真靶场那一支只在**真的要用它**的那一刻导入，
且失败要带着「去哪里跑」说清楚。

## `identifier` 为什么由 seed 派生

`probe_pool.py:18` 的原文：「`identifier` … **必须由实验 seed 派生**，
这样重跑出的字节逐次相同。」它是 ICMPv6 Echo 头里的 16 位字段，而我们
按 `(id, seq)` 匹配回包——两个 seed 撞同一个 id 会让两趟的回包互相认得。
取低 16 位而不是哈希，是为了让「seed → identifier」这一步**可以被一眼复核**；
撞 id 的代价被 `ProbePool` 自己的匹配规则兜住（回包要 id 与 seq 都对上）。
"""

from __future__ import annotations

from ..adapter.probe_pool import ProbePool
from .executors import ProbeExecutor

#: 本环境标定出的无丢包上界（`docs/perf_baseline.md`，`TODOS` 里那条 P2 的
#: 来源）。超过它不会报错——**丢包会表现为「这个地址没回包」，而那正好是
#: 实验要测量的那个量**。所以这里给一个保守的默认值，而不是留给调用方随手填。
CALIBRATED_POOL_CEILING = 512


def identifier_for_seed(seed: int) -> int:
    """`seed → ICMPv6 Echo identifier`（16 位）。理由见模块 docstring。"""
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError(f"seed 必须是整数，得到 {seed!r}")
    if not 0 <= seed:
        raise ValueError(f"seed 必须非负，得到 {seed}")
    return seed & 0xFFFF


def probe_executor(
    seed: int,
    *,
    max_in_flight: int,
    timeout_s: float = 1.0,
    recv_bufsize: int = 1 << 20,
) -> ProbeExecutor:
    """造一个真网卡上的 executor（`ProbeExecutor` 协议）。

    Args:
        max_in_flight: 在途探测数。**必填无默认值**——与 `ProbePool` 同一条
            原则（V6-9）：漏传必须炸。它必须 ≤ 本环境标定的无丢包上界
            （`CALIBRATED_POOL_CEILING`），否则丢包会伪装成「地址不存在」。
            那条上界目前**只是一个文档里的数**，还没有会红的检查（`TODOS`
            P2）。

    Raises:
        RuntimeError: 不在 Linux 上，或者拿不到原始套接字权限。
    """
    if max_in_flight < 1:
        raise ValueError(f"max_in_flight 至少为 1，得到 {max_in_flight}")
    try:
        from ..adapter.iface import RawIcmp6Iface
    except Exception as exc:  # noqa: BLE001 - 非 Linux 上的失败形态不止一种
        raise RuntimeError(
            "真靶场的探测出口需要 Linux + CAP_NET_RAW，当前环境起不来 "
            f"（{type(exc).__name__}: {exc}）。\n"
            "开发机上请用 --offline（排练），或者把这次运行放进 "
            "`cd docker && docker compose --profile harness run --rm harness`。"
        ) from exc

    iface = RawIcmp6Iface(recv_bufsize=recv_bufsize)
    return ProbePool(
        iface,
        max_in_flight=max_in_flight,
        identifier=identifier_for_seed(seed),
        timeout_s=timeout_s,
    )


__all__ = ["CALIBRATED_POOL_CEILING", "identifier_for_seed", "probe_executor"]
