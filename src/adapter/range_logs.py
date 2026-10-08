"""读靶场自己的日志，与我们的发送计数对账（T2 / `Issue 3A`）。

## 为什么非对账不可

靶场队列满时会丢包，而**被丢的包在我们眼里就是「超时」**，超时的实验含义是
「地址不存在」。两者混淆会让整轮结论反向。设计方案把这条定为 critical gap：
`:1121` 「队列满丢包 → 伪装超时」**否（静默）**。

## 靶场到底记了什么（2026-10-08 读上游 `punchingbag.cpp` 得到，非推测）

上游只有**两个**日志汇点，没有第三个：

1. **`packet_log.txt`**（`:56` 打开、`:192` 写、`:386` 仅在退出时 flush）
   逐行 `时间戳微秒,地址`。**关键**：这一行写在队列满判断**之前**（`:192` 在
   `:194` 之前），所以它记录的是**网卡上抓到的每一个包**，包含后来被丢掉的。
2. **`queue_log_<时间戳>.txt`**（`:215` 起）1 Hz 采样队列深度，退出时写一段汇总。

**`packet_log.txt` 的存在是对设计方案 `:764` 那条 Open Question 的回答**——
那里问「逐包日志到底存不存在、什么格式」，并说「若不存在，Stage 0 的第一个任务
就是拿到逐包数据」。答案是存在，格式如上，不必再造。

## 三层丢失，各由谁看见

| 丢失发生在哪 | `packet_log` 有吗 | `queue_log` 有吗 | 我们的对账看见吗 |
|---|---|---|---|
| pcap 缓冲区溢出（回调压根没被调用） | 否 | 否 | **能**（`captured < sent`） |
| 队列满，回调早退不 push（`:194-198`） | **是** | 否（`recieved_packets` 不 ++） | **能**（`enqueued < captured`） |
| 决定回包但 `libnet_write` 失败 | — | 是（`Libnet errors`） | **能**（`libnet_errors > 0`） |

第一层最危险：**它自己在任何日志里都不留痕**，唯一的证据是我们这边的 `sent`
比靶场见过的多。所以对账必须拿 `sent` 去比，而不是只读靶场的日志自证。

> **注意第二层的连带效应**：队列满时回调里 `sleep(1s)` 才返回（`:196`），
> 这一秒里 pcap 收不到任何东西，缓冲区溢出的包就直接落进第一层——**看不见**。
> 两层不是独立的，高负载下第一层会被第二层放大。

## 汇总行里有个拼写陷阱

实时行写 `Received packets: N`，而退出汇总写 `Recieved packets: N`（**少一个 i**，
上游 `:275`）。按 `"Received packets"` 去找汇总会**静默**取到实时行的旧值，
而那个值偏小——正好把丢包算得更多，方向虽保守但数字是错的。所以下面两个
模式分开写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 实时行：`[…]: Queue size: 12, Usage: 0.0048%, Received packets: 34567, Libnet errors: 0`
_SAMPLE_RE = re.compile(
    r"Queue size:\s*(?P<queue>\d+),\s*"
    r"Usage:\s*(?P<usage>[\d.]+)%,\s*"
    r"Received packets:\s*(?P<received>\d+),\s*"
    r"Libnet errors:\s*(?P<errors>\d+)"
)

#: 退出汇总。**`Recieved` 是上游的拼写**（`punchingbag.cpp:275`），不是笔误。
_SUMMARY_ENQUEUED_RE = re.compile(r"^Recieved packets:\s*(?P<n>\d+)\s*$", re.MULTILINE)
_SUMMARY_QUEUE_RE = re.compile(r"^Final queue size:\s*(?P<n>\d+)\s*$", re.MULTILINE)
_SUMMARY_ERRORS_RE = re.compile(r"^Libnet errors:\s*(?P<n>\d+)\s*$", re.MULTILINE)


class RoundInvalid(Exception):
    """这一轮探测**不可用**——实收对不上已发。

    它必须是一个显式的失败，不得降级成「有些超时而已」：被丢的包与「地址不回包」
    在数据里长得一模一样，唯一能把它们分开的就是这次对账。
    """


@dataclass(frozen=True, slots=True)
class QueueSample:
    """一条 1 Hz 采样。"""

    queue_size: int
    usage_percent: float
    received: int
    libnet_errors: int


@dataclass(frozen=True, slots=True)
class RangeLedger:
    """靶场侧能拿到的全部计数。"""

    #: 进入队列的包数（上游 `recieved_packets` 的终值）。**已含被丢的之外的全部。**
    enqueued: int
    final_queue_size: int
    libnet_errors: int
    #: `packet_log.txt` 的行数 —— 网卡上真正被靶场抓到的包数。
    captured: int
    samples: tuple[QueueSample, ...] = ()
    #: 汇总行是否读到。**没读到说明靶场不是正常退出的**，此时所有终值都不可信。
    has_summary: bool = False

    @property
    def queue_dropped(self) -> int:
        """队列满被丢的包数（第二层）。"""
        return self.captured - self.enqueued

    def reconcile(self, *, sent: int, expect_clean: bool = True) -> None:
        """把「我们发了多少」与「靶场看见/入队了多少」对上。

        Args:
            sent: 我们成功 `sendto` 的包数（`ProbeRun.sent`）。**不含 `send_failed`。**
            expect_clean: 是否要求完全相等。容器环境的容差口径见设计方案判据 6(ii)，
                但那条容差是给**回包计数**的，不是给**入队计数**的——丢包没有容差。

        Raises:
            RoundInvalid: 任何一层丢失非零，或汇总缺失。
        """
        if expect_clean and not self.has_summary:
            raise RoundInvalid(
                "没读到靶场的退出汇总 —— 靶场不是正常退出的，终值不可信。"
                "多半是被 kill 掉的；对账必须在 SIGTERM 之后、flushed 之后进行"
            )

        problems: list[str] = []
        if self.captured < sent:
            problems.append(
                f"{sent - self.captured} 个包**靶场从没看见**（pcap 缓冲区溢出，"
                f"日志里不留痕）：已发 {sent}，抓到 {self.captured}"
            )
        if self.enqueued < self.captured:
            problems.append(
                f"{self.queue_dropped} 个包**队列满被丢**（回调早退，"
                f"`recieved_packets` 不计数）：抓到 {self.captured}，入队 {self.enqueued}"
            )
        if self.captured > sent:
            # 反过来也不对：多余说明有别的发送方，那 pair 的假设就不成立了。
            problems.append(
                f"靶场抓到的比我们发的还多 {self.captured - sent} 个 —— "
                "有别的进程也在往靶场发包，对账的前提不成立"
            )
        if self.libnet_errors > 0:
            problems.append(
                f"靶场决定回包但有 {self.libnet_errors} 个 `libnet_write` 失败 —— "
                "这些包的「没回」不是地址的原因"
            )

        if problems:
            raise RoundInvalid(
                "这一轮不可用：\n  - " + "\n  - ".join(problems) + "\n"
                "被丢的包在数据里与「地址不回包」完全同形，所以不能只把它当超时。"
                "降低并发池大小后重跑（上界见 docs/perf_baseline.md）。"
            )


def parse_queue_log(text: str) -> RangeLedger:
    """解析一个 `queue_log_*.txt` 的内容。"""
    samples = tuple(
        QueueSample(
            queue_size=int(m["queue"]),
            usage_percent=float(m["usage"]),
            received=int(m["received"]),
            libnet_errors=int(m["errors"]),
        )
        for m in _SAMPLE_RE.finditer(text)
    )

    enqueued = _SUMMARY_ENQUEUED_RE.search(text)
    final_queue = _SUMMARY_QUEUE_RE.search(text)
    errors = _SUMMARY_ERRORS_RE.search(text)

    return RangeLedger(
        enqueued=int(enqueued["n"]) if enqueued else (samples[-1].received if samples else 0),
        final_queue_size=int(final_queue["n"]) if final_queue else 0,
        libnet_errors=int(errors["n"]) if errors else 0,
        captured=0,  # 由调用方用 packet_log 补上
        samples=samples,
        has_summary=enqueued is not None and final_queue is not None,
    )


def count_captured(packet_log: Path) -> int:
    """数 `packet_log.txt` 的行数 —— 靶场从网卡上真正抓到的包数。

    行格式 `时间戳微秒,地址`，一行一包。空行不计。
    """
    if not packet_log.exists():
        raise FileNotFoundError(
            f"找不到 {packet_log}。它是靶场在**自己的 CWD** 里创建的"
            "（`punchingbag.cpp:56`），要读到它得让靶场的 CWD 与这里共享卷"
        )
    with packet_log.open("r", encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh if line.strip())


def find_latest_queue_log(directory: Path) -> Path:
    """挑出最新的 `queue_log_*.txt`。

    文件名带时间戳，同一目录里会堆多个——**取名字最大的**（`%Y-%m-%d_%H-%M-%S`
    字典序即时间序），而不是 mtime：mtime 会被拷贝/挂载改写。
    """
    candidates = sorted(directory.glob("queue_log_*.txt"))
    if not candidates:
        raise FileNotFoundError(
            f"{directory} 下没有 queue_log_*.txt。靶场没起来，或者它的 CWD 不在那儿"
        )
    return candidates[-1]


def read_ledger(range_dir: Path) -> RangeLedger:
    """从靶场的日志目录读出一份完整账本。"""
    ledger = parse_queue_log(find_latest_queue_log(range_dir).read_text(encoding="utf-8"))
    return RangeLedger(
        enqueued=ledger.enqueued,
        final_queue_size=ledger.final_queue_size,
        libnet_errors=ledger.libnet_errors,
        captured=count_captured(range_dir / "packet_log.txt"),
        samples=ledger.samples,
        has_summary=ledger.has_summary,
    )
