"""线上抓包对账 —— 第四层丢包**长什么样**（T2(b)）。

## 这个脚本存在的唯一理由

靶场的三层账本（pcap 溢出 / 队列满 / libnet 错）在 `pool ≥ 4096` 那一档
**全部报干净**：`captured == enqueued == sent`、`libnet_errors == 0`。
而同一轮的 `run.received` 只有 4157/20000。

「账本干净」与「我们只收到两成」同时成立时，少掉的那 15843 个回包
**在靶场的任何日志里都不留痕，在我们的任何计数里也不留痕**——它们只存在于
「靶场决定回」与「我们没有读到」这两句话之间的那段路上。三个候选：

    ① 靶场根本发不出去（`libnet_errors` 记账覆盖不到的部分）
    ② 发了，在 bridge/veth 上丢了
    ③ 到了本容器的 eth0，被内核在我们 recvmsg 之前丢掉了

**这三条的应用层症状完全一样**（都是「这个地址没回包」），而实验含义相反：
①/② 是靶场的问题（换环境），③ 是我们自己的问题（改代码）。靠推理分不开，
所以在 eth0 上抓一份**独立于我们那个套接字**的帧计数。

## 为什么统计的是「突发形状」而不只是总数

实测下来丢包与 `SO_RCVBUF` **不是**简单的容量关系：8 KiB 缓冲区也拿到了
8185/20000，而把它加到 416 KiB 只到 17044。说明丢的不是「缓冲区装不下总量」，
而是**某一瞬间装不下**——回包是一簇一簇到的（靶场的 pcap 读超时 100 ms +
`--thread-count=N` 的批处理），簇的高度决定了丢多少。
所以这里报 `max_frames_in_1/5/100ms`：**最猛的一瞬间有多少帧同时到**。
它才是与 `effective_rcvbuf / 帧长` 直接可比的那个数。

## 为什么用 `-tt` 而不是 `-w`

`-w` 写 pcap 再多读一次文件，多一层自己的丢弃风险（而且 pcap 文件的丢包
统计得自己解析）。这里只要时间戳，`-tt` 的文本行第一列就是 Unix 秒，
`-l` 行缓冲保证被 kill 之前已经落盘。
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

# 复用标定脚本的地址生成与路由——**必须复用**：两份不同的地址生成会让
# 「线上抓到的帧数」与「标定的 received」不再对应同一个目标集合，
# 那样这个脚本的证据就断了。
from scripts.perf_calibrate import IDENTIFIER, addresses, ensure_route

from src.adapter.iface import RawIcmp6Iface
from src.adapter.probe_pool import ProbePool, ProbeTarget

#: 只要 Echo Reply（type 129）。发出去的 Echo Request 是 128，
#: 邻居发现/路由通告是 135/136——不滤掉它们，「线上帧数」里会混进
#: 与本轮探测无关的流量，而这个脚本的全部意义就是把**这一轮**数准。
_FILTER = "icmp6 and ip6[40] == 129"

#: `-tt` 输出的第一列：`1696800000.123456 IP6 …`
_TS_RE = re.compile(r"^(\d+\.\d+)")


def start_capture(iface: str, out_path: Path, buffer_kib: int = 8192) -> subprocess.Popen:
    """起 tcpdump，把带时间戳的文本行写到 `out_path`。

    `-B` 给的是 **KiB**。默认给 8 MiB：抓包工具自己丢帧会**假报**成
    「网络丢了」，而它与内核丢包在文本输出里长得一样（都只是少了些行）。
    所以在 `tail` 里核对 "packets dropped by kernel"。
    """
    handle = out_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        [
            "tcpdump",
            "-i", iface,
            "-n", "-tt", "-l",
            "-B", str(buffer_kib),
            "-s", "128",
            _FILTER,
        ],
        stdout=handle,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_timestamps(path: Path) -> list[float]:
    out: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TS_RE.match(line.strip())
        if m:
            out.append(float(m.group(1)))
    out.sort()
    return out


def burst_stats(ts: list[float]) -> dict:
    """把到达时间戳整理成「簇」的形状。

    三个桶宽不是随手取的：1 ms 是**我们轮询节拍的量级**（池子每轮睡 1 ms），
    5 ms 是单次 `recv_available()` 能覆盖的窗口，100 ms 是**靶场的 pcap 读超时**
    ——回包正是在这个节拍上被批量放出来的。三者一比就能看出丢包是被哪一端决定的。
    """
    if not ts:
        return {"frames": 0}
    span = ts[-1] - ts[0]
    gaps = sorted((ts[i + 1] - ts[i]) * 1_000.0 for i in range(len(ts) - 1))

    def widest(bucket_ms: float) -> int:
        buckets = Counter(int(t / (bucket_ms / 1000.0)) for t in ts)
        return max(buckets.values())

    def pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        return round(values[min(len(values) - 1, int(len(values) * p))], 4)

    return {
        "frames": len(ts),
        "span_s": round(span, 4),
        "max_frames_in_1ms": widest(1.0),
        "max_frames_in_5ms": widest(5.0),
        "max_frames_in_100ms": widest(100.0),
        "gap_p50_ms": pct(gaps, 0.5),
        "gap_p99_ms": pct(gaps, 0.99),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf_wire")
    parser.add_argument("--pool-size", type=int, required=True)
    parser.add_argument("--count", type=int, default=20000)
    parser.add_argument("--prefix", default="2001:db8:dead::/64")
    parser.add_argument("--recv-bufsize", type=int, default=1 << 20)
    parser.add_argument("--iface", default="eth0")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="抓包起来之后等多久再开跑。太短会把**启动那几毫秒**漏掉，"
        "而丢包若发生在开跑瞬间就被漏掉了",
    )
    parser.add_argument(
        "--tail",
        type=float,
        default=4.0,
        help="跑完再抓这么久。回包可能比 `run` 返回还晚到（池子在最后一个探测"
        "结算完就退出），不给这段尾巴会把「迟到」误记成「没到」",
    )
    args = parser.parse_args(argv)

    ensure_route(args.prefix)
    targets = [ProbeTarget(a) for a in addresses(args.prefix, args.count)]

    with tempfile.TemporaryDirectory() as tmp:
        cap_path = Path(tmp) / "wire.txt"
        proc = start_capture(args.iface, cap_path)
        try:
            time.sleep(args.settle)

            with RawIcmp6Iface(recv_bufsize=args.recv_bufsize) as iface:
                effective = iface.effective_recv_bufsize
                pool = ProbePool(
                    iface,
                    max_in_flight=args.pool_size,
                    identifier=IDENTIFIER,
                    timeout_s=args.timeout,
                )
                started = time.monotonic()
                run = pool.run(targets)
                elapsed = time.monotonic() - started

            time.sleep(args.tail)
        finally:
            # SIGINT 让 tcpdump 打它的汇总行（含 dropped by kernel），
            # 比 SIGKILL 多一条**自证没丢帧**的证据。
            proc.send_signal(signal.SIGINT)
            try:
                _, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                _, stderr = proc.communicate()

        ts = read_timestamps(cap_path)

    wire = burst_stats(ts)
    # **这里只报帧的「个数」与「到达时刻」，不报字节。** 因为要与 `effective_rcvbuf`
    # 对上的那个量是内核的 skb `truesize`（约 800 字节/包），而不是线上帧长
    # （62 字节）——拿帧长去除缓冲区会算出 7000 个的容量，比实际大一个数量级。
    # `truesize` 抓包看不到，所以这一层只能靠 `received` 与池大小的关系去反推，
    # 见 `docs/perf_baseline.md` §4.3。
    report = {
        "pool_size": args.pool_size,
        "count": args.count,
        "sent": run.sent,
        "socket_received": run.received,
        "socket_no_reply": run.no_reply,
        "elapsed_s": round(elapsed, 4),
        "pps": round(run.sent / elapsed, 1) if elapsed > 0 else None,
        "requested_rcvbuf": args.recv_bufsize,
        "effective_rcvbuf": effective,
        "wire": wire,
        "tcpdump_dropped_by_kernel": _dropped(stderr),
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _dropped(stderr: str | None) -> int | None:
    """从 tcpdump 的汇总行里抠出它自己丢了多少帧。

    **必须报出来**：抓包工具丢帧与网络丢帧在我们的文本里同形。不报这一项，
    「线上只有 8000 帧」既可能是网络真丢了，也可能是 tcpdump 自己没接住,
    而后者会让整个结论反过来。
    """
    if not stderr:
        return None
    for line in stderr.splitlines():
        if "dropped by kernel" in line:
            digits = re.search(r"(\d+)\s+packets dropped", line)
            if digits:
                return int(digits.group(1))
    return None


if __name__ == "__main__":
    sys.exit(main())
