"""吞吐标定 —— **宿主侧编排**（T2(b)）。

扫一遍在途探测数，逐档记 pps 与**三层丢包**，取「无丢包」的最大值作为该环境
的池大小上界（设计方案 `:1256`）。产出 `docs/perf_baseline.md` 的素材表。

    cd docker && python ../scripts/perf_sweep.py

## 为什么这一档必须是「起 → 跑 → **停** → 读」

`packet_log.txt` **只在正常退出时 flush**（`punchingbag.cpp:386`）。在靶场还
活着的时候去读它，读到的是被截断的文件——于是「我们发了 2000、文件里只有
300 行」会被对账读成**第一层丢包 1700 个**（pcap 缓冲区溢出），而那 1700
个丢包**根本不存在**。

**这个假报的方向最坏**：它把结果推向「这个环境很慢、池子要开得很小」，
而那是一个**保守**结论——保守结论不会有人去质疑它，所以它会留下来。

所以每一档固定四步，且顺序不可换：

    ① recreate target（清空日志目录，拿到干净账本）
    ② 跑探测（harness 容器里，`scripts/perf_calibrate`）
    ③ **stop target**（SIGTERM → 正常退出 → flush）
    ④ 读账本（宿主直接读 bind mount 进来的目录）

## 为什么用 `has_summary` 当「正常退出」的判据

靶场退出时写一段汇总（`Final queue size` / `Recieved packets`）。读得到它，
就说明退出路径走完了；读不到，说明它是被 kill 的——此时**所有终值都不可信**，
这一档必须作废而不是记一个偏小的数。这条判据不靠我们猜，是 `range_logs`
里已经实现好的（`RangeLedger.has_summary`）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = REPO_ROOT / "docker"
LOG_DIR = DOCKER_DIR / "run" / "target-logs"
COMPOSE = ["docker", "compose"]

sys.path.insert(0, str(REPO_ROOT))
from src.adapter.range_logs import RoundInvalid, read_ledger  # noqa: E402

#: 靶场起来之后要等多久才开跑。`depends_on` 只等容器启动，不等 pcap 打开；
#: 还没打开时发的包会被丢掉，而症状是「一发就丢」——正好伪装成本次要测的东西。
SETTLE_S = 3.0

#: SIGTERM 之后给靶场的宽限。要够它写完汇总行，但不能长到掩盖「它卡住了」。
STOP_GRACE_S = 10


def run(cmd: list[str], *, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=DOCKER_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"命令失败（退出码 {result.returncode}）: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    if not quiet:
        print(f"  $ {' '.join(cmd)}  → {result.returncode}", flush=True)
    return result


def fresh_target() -> None:
    """① recreate：先把靶场和它的日志目录都清干净。"""
    run([*COMPOSE, "rm", "-sf", "target"], check=False, quiet=True)
    if LOG_DIR.exists():
        # 用 Python 删而不是 `docker run --rm -v ... rm -rf`：少一次容器启动，
        # 也免得在 Docker Desktop 上把宿主目录的属主搅乱。
        shutil.rmtree(LOG_DIR, ignore_errors=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run([*COMPOSE, "up", "-d", "target"])
    time.sleep(SETTLE_S)


def probe(pool_size: int, count: int, prefix: str) -> dict:
    """② 跑探测。返回 harness 容器里那行 JSON。"""
    result = run(
        [
            *COMPOSE,
            "run",
            "--rm",
            "harness",
            "python",
            "-m",
            "scripts.perf_calibrate",
            "--pool-size",
            str(pool_size),
            "--count",
            str(count),
            "--prefix",
            prefix,
        ]
    )
    line = next(
        (ln for ln in reversed(result.stdout.splitlines()) if ln.strip().startswith("{")),
        None,
    )
    if line is None:
        raise SystemExit(f"没读到标定结果 JSON。stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return json.loads(line)


def stop_target() -> None:
    """③ 停靶场。**这一步不能省**，理由见模块 docstring。"""
    run([*COMPOSE, "stop", "-t", str(STOP_GRACE_S), "target"])
    run([*COMPOSE, "rm", "-f", "target"], check=False, quiet=True)


def read_range_ledger(sent: int, received: int) -> dict:
    """④ 读账本，把**四层**丢包各算一遍。

    这里**不抛** `RoundInvalid`——标定的目的正是「找到丢包从哪一档开始」，
    遇到丢包就抛会让我们只拿到「第一档就炸了」这一个信息。改为把各层的
    数字原样报出来，判定留给读表的人。

    ## 第四层：回包在回来的路上丢的（v7 记录的那一条）

    前三层**全部是请求路径上的**：pcap 收没收到、队列装没装下、libnet 写没写成。
    回包路径上还有一层，而靶场的账本对它**完全瞎**——它只记账「我决定要回这个包」，
    不记账「这个包到了对面没有」。

    实测（2026-10-08，T2(b)）：`pool ≥ 4096` 那一档，靶场账本报
    `captured == enqueued == 20000`、`libnet_errors == 0`，**三层全清**；
    而同一轮我们只收到 4157 个回包。在 eth0 上独立抓包证实 20000 个回包
    **全部到达了本机网卡**——丢在容器内核的套接字接收队列里，因为回包是
    **簇状**到达的（靶场的 pcap 读超时 100 ms，批量应答；实测 20000 个回包
    在 276 ms 内到齐，峰值 477 帧/ms），而队列只有 425984 字节。

    所以 `sent - received` 是这个脚本里**唯一**能看见第四层的式子。
    只看三层的 `clean` 会给一个丢了两成回包的档位盖章「无丢包」，
    而那正是标定要避免的错——把「我们没接住」记成「地址不存在」。
    """
    ledger = read_ledger(LOG_DIR)
    info = {
        "has_summary": ledger.has_summary,
        "captured": ledger.captured,
        "enqueued": ledger.enqueued,
        # 第一层：pcap 缓冲区溢出。**靶场的任何日志里都不留痕**，
        # 唯一的证据就是我们这边的 sent。所以这一项必须拿 sent 来比。
        "lost_pcap": max(0, sent - ledger.captured),
        # 第二层：队列满，回调早退不 push（`:194-198`）。
        "lost_queue": max(0, ledger.captured - ledger.enqueued),
        # 第三层：决定回包但 libnet_write 失败。
        "libnet_errors": ledger.libnet_errors,
        "extra_traffic": max(0, ledger.captured - sent),
        # 第四层：回包路径。见 docstring——靶场的账本对它完全瞎。
        "lost_reply_path": max(0, sent - received),
    }
    # 靶场自己的三层。**它干净不等于这一档干净**，见 docstring。
    info["range_clean"] = (
        info["has_summary"]
        and info["lost_pcap"] == 0
        and info["lost_queue"] == 0
        and info["libnet_errors"] == 0
        and info["extra_traffic"] == 0
    )
    # 这一档能不能用，看的是**四层**。
    # `lost_reply_path == 0` 是一条**经验**判据，不是推导出来的：它成立的前提是
    # 靶场配成 rate 1.0（对每个地址都回），于是「实收 < 已发」只可能是丢了。
    # 换配置时必须重判——这条写在 docs/perf_baseline.md 里。
    info["clean"] = info["range_clean"] and info["lost_reply_path"] == 0
    try:
        ledger.reconcile(sent=sent)
        info["verdict"] = "ok"
    except RoundInvalid as exc:
        info["verdict"] = str(exc).splitlines()[0]
    return info


def sweep(levels: list[int], count: int, prefix: str) -> list[dict]:
    rows: list[dict] = []
    for level in levels:
        print(f"\n=== 池大小 {level} ===", flush=True)
        fresh_target()
        measured = probe(level, count, prefix)
        stop_target()
        ledger = read_range_ledger(measured["sent"], measured["received"])
        rows.append({**measured, **ledger})
        print(
            f"  → sent={measured['sent']} received={measured['received']} "
            f"pps={measured['pps']} 靶场三层(pcap/队列/libnet)="
            f"{ledger['lost_pcap']}/{ledger['lost_queue']}/{ledger['libnet_errors']} "
            f"靶场自评={ledger['range_clean']} "
            f"回包路径丢={ledger['lost_reply_path']} clean={ledger['clean']}",
            flush=True,
        )
    return rows


def markdown(rows: list[dict]) -> str:
    head = (
        "| 在途数 | 已发 | 实收 | 无回包 | pps | 耗时 s | pcap 层丢 | 队列层丢 | "
        "libnet 错 | 回包路径丢 | 靶场自评 | 结论 |\n"
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|\n"
    )
    # 表头之上先把**这一轮到底用了什么**写死：`SO_RCVBUF` 与 `drain_every_sends`
    # 都直接决定丢多少，不记下来这张表就换不了环境复现。
    first = rows[0] if rows else {}
    preamble = (
        f"环境：SO_RCVBUF 请求 {first.get('requested_rcvbuf')} → 实得 "
        f"{first.get('effective_rcvbuf')} 字节；drain_every_sends="
        f"{first.get('drain_every_sends')}；poll_interval_s={first.get('poll_interval_s')}"
        f"；count={first.get('count')}；prefix={first.get('prefix')}\n\n"
    )
    body = "".join(
        f"| {r['pool_size']} | {r['sent']} | {r['received']} | {r['no_reply']} | {r['pps']} | "
        f"{r['elapsed_s']} | {r['lost_pcap']} | {r['lost_queue']} | {r['libnet_errors']} | "
        f"{r['lost_reply_path']} | {'干净' if r['range_clean'] else '有丢'} | "
        f"{'**无丢包**' if r['clean'] else '有丢包'} |\n"
        for r in rows
    )
    return preamble + head + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf_sweep")
    parser.add_argument(
        "--levels",
        default="1,4,16,64,256,1024",
        help="要扫的在途数，逗号分隔。**不要把池大小固定在某一档**：上界是靠对比出来的",
    )
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--prefix", default="2001:db8:dead::/64")
    parser.add_argument("--json-out", type=Path, help="把原始读数也存一份")
    args = parser.parse_args(argv)

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    rows = sweep(levels, args.count, args.prefix)

    table = markdown(rows)
    print("\n" + table)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"原始读数已写到 {args.json_out}")

    clean = [r["pool_size"] for r in rows if r["clean"]]
    if clean:
        print(f"\n**无丢包的最大在途数：{max(clean)}**")
    else:
        print("\n**没有任何一档无丢包** —— 上界低于最小档，标定不成立，要降 count 或查环境")
    return 0


if __name__ == "__main__":
    sys.exit(main())
