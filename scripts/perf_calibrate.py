"""吞吐标定 —— **跑在 harness 容器里的一档**（T2(b)）。

宿主侧的编排在 `scripts/perf_sweep.py`，它每一档把本脚本叫一次。
**分工的理由**：靶场的 `packet_log.txt` 只在正常退出时 flush
（`punchingbag.cpp:386`），所以「停靶场」必须发生在「读账本」之前，而那两步
只有宿主做得到。

    docker compose run --rm harness \
        python -m scripts.perf_calibrate --pool-size 64 --count 2000

输出一行 JSON 到 stdout。**探测量本身不含任何判定**——丢没丢包由宿主侧读
靶场账本对出来（`src/adapter/range_logs.py` 的三层）。这里只负责「把这一档
按指定的在途数跑完，报出我发了多少、耗时多少」。

## 为什么池大小必填、无默认值

同 `ProbePool`（V6-9）：池大小同时是性能旋钮与「整轮作废」的触发条件。
标定脚本给它一个默认值，等于把「这一档到底用了几路并发」写成了静默行为
——而**那正是这份文档要回答的问题**。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import time

from src.adapter.iface import RawIcmp6Iface
from src.adapter.probe_pool import ProbePool, ProbeTarget

#: 靶场容器的地址（compose 写死）。探测前缀要显式路由到它——理由见 compose
#: 文件顶部第 2 条：`pcap_open_live` 是**非混杂**的，不加路由则 L2 目的 MAC
#: 永远问不到，一个包都发不出去，症状却是「靶场不回包」。
TARGET_IPV6 = "2001:db8:1::2"

#: 本次标定用的 identifier。**写死常量而不是随机**：回包要能跨 run 稳定匹配，
#: 而基于 pid/时间的默认值会让同一档重跑出不同的匹配行为。
IDENTIFIER = 0x4D54

#: 地址生成用的步长。取一个与 2^64 互质的奇数，于是 `i * STEP mod 2^64` 在
#: 前 2^64 个 i 上**不重复**——标定不需要「像真实 hitlist 那样分布」，
#: 只需要「每个探测打在不同的地址上」，避免把结果混淆成「同一个地址被反复探」。
_STEP = 0x9E3779B97F4A7C15


def addresses(prefix: str, count: int) -> list[str]:
    """在 `prefix` 里生成 `count` 个互不相同的地址（确定性的）。"""
    net = ipaddress.ip_network(prefix, strict=False)
    width = net.max_prefixlen - net.prefixlen
    if width < 1:
        raise ValueError(f"{prefix} 里放不下多个地址")
    base = int(net.network_address)
    span = 1 << width
    out: list[str] = []
    seen: set[int] = set()
    for i in range(count):
        offset = (i * _STEP) % span
        if offset == 0:
            # 全零主机位是 subnet-router anycast，某些栈对它的处理与普通单播
            # 不同；标定不该被这个特例污染。
            offset = 1
        while offset in seen:
            offset = (offset + 1) % span
        seen.add(offset)
        out.append(str(ipaddress.ip_address(base + offset)))
    return out


def ensure_route(prefix: str) -> None:
    """把探测前缀指到靶场。已存在不是错误（重复跑的正常情况）。"""
    result = subprocess.run(
        ["ip", "-6", "route", "add", prefix, "via", TARGET_IPV6],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "File exists" not in result.stderr:
        raise SystemExit(f"加路由 {prefix} 失败: {result.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf_calibrate")
    parser.add_argument("--pool-size", type=int, required=True, help="在途探测数（必填，无默认值）")
    parser.add_argument("--count", type=int, default=2000, help="这一档发多少个探测")
    parser.add_argument("--prefix", default="2001:db8:dead::/64")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument(
        "--recv-bufsize",
        type=int,
        default=1 << 20,
        help="请求的 SO_RCVBUF。**它是这条链路上真正的瓶颈**，见 docs/perf_baseline.md",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="覆盖 ProbePool 的轮询间隔（秒）。设 0 = 忙等排空，用来判定"
        "「高并发下的丢包是不是池子自己的轮询节拍造成的」",
    )
    parser.add_argument(
        "--drain-every",
        type=int,
        default=None,
        help="覆盖 ProbePool 的 drain_every_sends（填窗途中每发多少包收一次）。"
        "**这一项直接决定丢掉多少回包**，见 docs/perf_baseline.md",
    )
    args = parser.parse_args(argv)

    if args.pool_size < 1:
        raise SystemExit(f"--pool-size 至少为 1，得到 {args.pool_size}")

    ensure_route(args.prefix)
    targets = [ProbeTarget(a) for a in addresses(args.prefix, args.count)]

    pool_kwargs = {}
    if args.poll_interval is not None:
        pool_kwargs["poll_interval_s"] = args.poll_interval
    if args.drain_every is not None:
        pool_kwargs["drain_every_sends"] = args.drain_every

    with RawIcmp6Iface(recv_bufsize=args.recv_bufsize) as iface:
        effective_rcvbuf = iface.effective_recv_bufsize
        pool = ProbePool(
            iface,
            max_in_flight=args.pool_size,
            identifier=IDENTIFIER,
            timeout_s=args.timeout,
            **pool_kwargs,
        )
        started = time.monotonic()
        run = pool.run(targets)
        elapsed = time.monotonic() - started

    print(
        json.dumps(
            {**_measure(run, args, elapsed), "requested_rcvbuf": args.recv_bufsize,
             "effective_rcvbuf": effective_rcvbuf,
             "drain_every_sends": pool.drain_every_sends,
             "poll_interval_s": pool.poll_interval_s},
            ensure_ascii=False,
        )
    )
    return 0


def _measure(run, args, elapsed: float) -> dict:
    """把一档的读数整理成一行。

    **`rtt_ms` 的分布是这份标定里最要紧的一列**，比吞吐本身要紧：它把
    「回包真的慢」与「我们的循环慢」分开。两者都会让吞吐掉下来，但含义相反
    ——前者是这个靶场的固有属性（要写进实验口径），后者是我们自己的 bug。
    只报 pps 的话，两种情况的读数**长得一模一样**。
    """
    rtts = sorted(r.rtt_ms for r in run.responses if r.rtt_ms is not None)
    return {
        "pool_size": args.pool_size,
        "count": args.count,
        "prefix": args.prefix,
        "sent": run.sent,
        "send_failed": run.send_failed,
        "received": run.received,
        "no_reply": run.no_reply,
        "foreign_replies": run.foreign_replies,
        "elapsed_s": round(elapsed, 4),
        "pps": round(run.sent / elapsed, 1) if elapsed > 0 else None,
        # 每一轮「填窗 → 收 → 判超时」的周期里，每个目标平均摊到多少时间。
        # 与 `rtt_*` 一比就能定位延迟在谁那边。
        "cycle_ms": round(elapsed / max(run.sent, 1) * 1000, 3),
        "rtt_min_ms": round(rtts[0], 3) if rtts else None,
        "rtt_p50_ms": round(rtts[len(rtts) // 2], 3) if rtts else None,
        "rtt_max_ms": round(rtts[-1], 3) if rtts else None,
    }


if __name__ == "__main__":
    sys.exit(main())
