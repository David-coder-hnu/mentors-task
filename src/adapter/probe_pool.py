"""并发探测池 —— `ProbeSurface` 底下的调度与超时判定（T2）。

**这一层回答的问题**：一批目标地址发出去之后，哪些回了、哪些没回、哪些**根本没发出去**。
第三类是最容易被吞掉的：发包失败一旦塌进「没回」，丢包对账就会把「网卡没起来」
记成「地址不存在」，而这两件事的实验含义相反（设计方案 `:338`）。

## 池大小没有默认值（V6-9）

`max_in_flight` **必须显式传入**。理由不是风格：池大小同时是性能旋钮
（`Issue 5A`）和「整轮作废」的触发条件（`Issue 3A`，靶场队列满即丢包）。
给它一个默认值，等于把「整轮作废」写成**静默行为**——调用方漏传时不会报错，
只会得到一批看起来像「地址不存在」的超时。所以这里宁可让漏传变成 `TypeError`。

## `identifier` 也没有默认值

同理但更微妙：两个池实例若共用 identifier，回包会**交叉匹配**。
而任何基于 `pid` / 时间 / `random` 的默认值都会破坏可回放——
必须由实验 seed 派生，这样重跑出的字节逐次相同。

## 迟到回包不翻案

已判定超时的探测，若回包之后才到，**不把 `sent_no_reply` 改写成 `received`**，
而是记进 `ProbeRun.late_replies`。翻案会让结果取决于挂钟运气：同一份
`(config, seed)` 下，机器负载不同就会得到不同的回包集合，直接违反
判据 6(i) 的「逐次完全相同」。迟到是**环境问题**，交给上层对账，不在这里
偷偷抹平。

**迟到窗口默认是 0**（`late_window_s`）。这一条是必要的：池子在最后一个探测结算完
就退出，若不为迟到把循环多留一会儿，`late_replies` **永远是空的**——回包到得比
退出还晚。默认 0 的意思是**生产路径不付这份钱**（一次 run 结束时不多等），
需要这份证据的地方（标定、环境验收）显式把窗口打开。

## 超时用注入的时钟

`clock` / `sleep` 可注入，单测因此不依赖真实挂钟——否则「迟到回包」这类
路径只能靠 `sleep` 撞出来，测试要么慢要么飘。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, NamedTuple

from .iface import DEFAULT_PAYLOAD, RawIcmp6Iface, SendFailure
from .types import Response

#: 空转轮询间隔。池里还有在途探测、但这一瞬既没有回包也没到超时时，睡这么久。
DEFAULT_POLL_INTERVAL_S = 0.001

#: 默认超时。**有默认值是刻意的**：它不像池大小那样会让整轮作废——
#: 设小了只会把「慢回包」记成「没回」，而那正是 `late_replies` 要抓的东西。
DEFAULT_TIMEOUT_S = 1.0

#: 填窗途中每发这么多包就收一次回包。**这个值有默认值，且允许默认**：
#: 它只影响吞吐，不影响语义——收到哪些包仍然由「谁在这个时刻真的在队列里」
#: 决定，而那是环境的事。设大了会退化成「边喊边不听」（见 `run` 里 ① 的注释），
#: 所以在途数越大越要靠它；32 是在 T2(b) 的实测里取的，见 `docs/perf_baseline.md`。
DEFAULT_DRAIN_EVERY_SENDS = 32


class ProbeTarget(NamedTuple):
    """一次探测的目标。"""

    dst: str
    hop_limit: int = 64


@dataclass(frozen=True, slots=True)
class LateReply:
    """在判定超时**之后**才到的回包。

    它是环境漂移的证据，不是结果。上层的丢包对账读它来决定这轮能不能用。
    """

    dst: str
    src: str
    seq: int
    #: 距发包过了多久（毫秒）。
    delay_ms: float
    #: 超时阈值（毫秒）——迟到的判据是 `delay_ms > timeout_ms`。
    timeout_ms: float


@dataclass(frozen=True, slots=True)
class ProbeRun:
    """一轮探测的完整结果。

    `responses` 与输入的 targets **一一对应、顺序相同**——上层据此把
    `Response` 映射回它自己那套目标表，不需要再记 seq。
    """

    responses: tuple[Response, ...]
    late_replies: tuple[LateReply, ...] = ()
    #: 成功 `sendto` 的包数。
    sent: int = 0
    #: 发包失败的包数（`send_failed`）。**它不算进 `sent`。**
    send_failed: int = 0
    #: 收到的、不属于本轮的 ICMPv6 回包（别的 id、没见过的 seq、载荷不符）。
    foreign_replies: int = 0

    def __len__(self) -> int:
        return len(self.responses)

    @property
    def received(self) -> int:
        return sum(1 for r in self.responses if r.status == "received")

    @property
    def no_reply(self) -> int:
        return sum(1 for r in self.responses if r.status == "sent_no_reply")

    def by_status(self) -> dict[str, int]:
        out = {"received": 0, "sent_no_reply": 0, "send_failed": 0}
        for r in self.responses:
            out[r.status] += 1
        return out


@dataclass(slots=True)
class _Pending:
    seq: int
    index: int
    dst: str
    sent_at: float


class ProbePool:
    """滑窗式并发探测器。

    「滑窗」而不是「分波」：一波一次屏障会在波与波之间留下空档，把标定出的
    吞吐量系统性地压低——而标定正是这个池存在的另一半理由（T2(b)）。
    这里每收到一个回包就补发下一个，始终把在途数顶在 `max_in_flight` 上。
    """

    def __init__(
        self,
        iface: RawIcmp6Iface,
        *,
        max_in_flight: int,
        identifier: int,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        late_window_s: float = 0.0,
        payload: bytes = DEFAULT_PAYLOAD,
        verify_payload: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        drain_every_sends: int = DEFAULT_DRAIN_EVERY_SENDS,
    ) -> None:
        """
        Args:
            max_in_flight: 并发在途上限，**必填无默认值**（见模块 docstring）。
            identifier: ICMPv6 Echo identifier，**必填无默认值**；应由实验 seed
                派生，不要用 pid/时间/random，否则重跑不可回放。
            late_window_s: 超时之后**再多守这么久**，用来认领迟到回包。默认 0
                （不守）。生产路径要的是速度，标定/环境验收要的是这份证据，
                所以由调用方决定——见模块 docstring。
            verify_payload: 是否要求回包载荷与发出的一致才算认领。Stage 1 下
                靶场原样回抄（`punchingbag.cpp:178`），开着它能把「id/seq 撞车」
                挡掉。Stage 2 的 `RouterShim` 若不再回抄，把它关掉。
        """
        if max_in_flight < 1:
            raise ValueError(f"max_in_flight 至少为 1，得到 {max_in_flight}")
        if not 0 <= identifier <= 0xFFFF:
            raise ValueError(f"identifier 越界: {identifier}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正，得到 {timeout_s}")
        if late_window_s < 0:
            raise ValueError(f"late_window_s 不能为负，得到 {late_window_s}")
        if drain_every_sends < 1:
            raise ValueError(f"drain_every_sends 至少为 1，得到 {drain_every_sends}")

        self._iface = iface
        self._max_in_flight = max_in_flight
        self._identifier = identifier
        self._timeout_s = timeout_s
        self._payload = payload
        self._verify_payload = verify_payload
        self._clock = clock
        self._sleep = sleep
        self._poll_interval_s = poll_interval_s
        self._late_window_s = late_window_s
        self._drain_every_sends = drain_every_sends

        self._next_seq = 0

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    @property
    def identifier(self) -> int:
        return self._identifier

    @property
    def drain_every_sends(self) -> int:
        """填窗途中每发多少包收一次。**标定要能读到它**：它决定丢掉多少回包，
        读不到就没法把「这个环境的吞吐上界」与「这一轮用的节拍」分开记。"""
        return self._drain_every_sends

    @property
    def poll_interval_s(self) -> float:
        return self._poll_interval_s

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        """把目标跑完，返回与输入同序的 `ProbeRun`。

        写回 `send_failed` 而不是超时，是这一层的核心契约。
        """
        wanted = [t if isinstance(t, ProbeTarget) else ProbeTarget(t) for t in targets]
        out: list[Response | None] = [None] * len(wanted)
        pending: dict[int, _Pending] = {}
        # 已判超时、但仍在迟到窗口内——留着，好把之后到的回包认出来是「迟到」
        # 而不是「外来」。不进这个表就只能把两者混成一个计数。
        expired: dict[int, _Pending] = {}
        queue = deque(enumerate(wanted))
        late: list[LateReply] = []
        sent = 0
        send_failed = 0
        foreign = 0

        # `expired` 非空也要继续转：迟到窗口内还得守着，否则回包到得比退出还晚，
        # `late_replies` 永远是空的。`late_window_s=0` 时这个表当轮就清空，
        # 循环随即结束——生产路径不为它多等。
        def collect() -> None:
            """② 收：把套接字里此刻所有回包认领掉。

            抽成函数是为了能在**填窗途中**也叫它，见下面 ① 的长注释。
            """
            nonlocal foreign
            for got in self._iface.recv_available():
                msg = got.message
                if msg.identifier != self._identifier or msg.sequence is None:
                    foreign += 1
                    continue
                if self._verify_payload and got.payload != self._payload:
                    # 载荷不符说明这不是我们那个包（id/seq 只有 16 位，撞得掉）。
                    foreign += 1
                    continue

                probe = pending.pop(msg.sequence, None)
                if probe is None:
                    dead = expired.pop(msg.sequence, None)
                    if dead is None:
                        foreign += 1
                    else:
                        late.append(
                            LateReply(
                                dst=dead.dst,
                                src=got.src,
                                seq=msg.sequence,
                                delay_ms=(got.recv_monotonic - dead.sent_at) * 1000.0,
                                timeout_ms=self._timeout_s * 1000.0,
                            )
                        )
                    continue

                out[probe.index] = Response(
                    status="received",
                    dst=probe.dst,
                    src=got.src,
                    icmp_type=msg.icmp_type,
                    icmp_code=msg.icmp_code,
                    hop_limit=got.hop_limit,
                    rtt_ms=(got.recv_monotonic - probe.sent_at) * 1000.0,
                )

        #: 填窗循环里每发这么多包就**回头收一次**（见 ① 的注释）。
        since_drain = 0

        while queue or pending or expired:
            # ① 填窗：把在途数顶到上限。
            #
            # **填的过程中必须隔一阵收一次。** 这一条是实测逼出来的（T2(b)），
            # 不是风格：原先「先把整窗发满、再去收」的结构在 `pool ≥ 4096` 时
            # 丢掉七成回包，而**靶场账本与我们的三层对账都报干净**。
            #
            # 原因是两端节拍撞在一起：靶场的 pcap 读超时是 100 ms，它在每个节拍上
            # 把攒下的请求批量应答，于是回包以**簇**的形式到达（实测 pool=16384 时
            # 20000 个回包在 235 ms 内到齐，相邻帧间隔中位数 2.9 µs）。而填满
            # 16384 个在途要花几十毫秒，**这几十毫秒里我们一个包都不收**——
            # 内核的套接字接收队列只有 425984 字节（**按 skb 的 truesize 记账，
            # 一个 62 字节的回包占约 800 字节，所以只装得下约 500~550 个**），
            # 装不下的直接尾丢弃，进程内没有任何计数器会记这件事。
            #
            # 症状与「地址不存在」完全同形，于是丢包对账**祝福**了一个错结论。
            # 每 32 个包收一次，代价是每 32 次 `sendto` 多一次必然 `EAGAIN`
            # 的 `recvmsg`（队列空时立即返回），换来的是队列不再积累。
            while queue and len(pending) < self._max_in_flight:
                if since_drain >= self._drain_every_sends:
                    since_drain = 0
                    collect()
                index, target = queue.popleft()
                since_drain += 1
                seq = self._next_seq
                self._next_seq += 1
                try:
                    sent_at = self._iface.send_echo(
                        target.dst,
                        hop_limit=target.hop_limit,
                        identifier=self._identifier,
                        sequence=seq,
                        payload=self._payload,
                    )
                except SendFailure as exc:
                    # **不走超时路径。** 这是发包失败，与「发了没人回」是两件事。
                    out[index] = Response(
                        status="send_failed", dst=target.dst, error=exc.message
                    )
                    send_failed += 1
                    continue
                sent += 1
                pending[seq] = _Pending(seq, index, target.dst, sent_at)

            collect()

            # ③ 超时：到点的判为没回，移进迟到窗口。
            now = self._clock()
            for seq, probe in list(pending.items()):
                if now - probe.sent_at >= self._timeout_s:
                    out[probe.index] = Response(status="sent_no_reply", dst=probe.dst)
                    del pending[seq]
                    expired[seq] = probe
            for seq, probe in list(expired.items()):
                if now - probe.sent_at >= self._timeout_s + self._late_window_s:
                    del expired[seq]

            # ④ 有在途、或还在迟到窗口内守着时才睡。两者都空就直接退出，
            #    不白等一个轮询间隔。
            if pending or expired:
                self._sleep(self._poll_interval_s)

        # 「与输入同序、一一对应」是 `ProbeRun` 对外的承诺。若哪个索引没填上，
        # 过滤掉它就会**静默**破坏这个承诺，让上层把结果错配到别的目标上。
        # 这是内部不变量的破坏，必须炸。
        unfilled = [i for i, r in enumerate(out) if r is None]
        if unfilled:  # pragma: no cover - 走到这里就是 probe_pool 自己有 bug
            raise AssertionError(
                f"内部错误：{len(unfilled)} 个目标没有结果（索引 {unfilled[:5]}）"
            )

        return ProbeRun(
            responses=tuple(r for r in out if r is not None),
            late_replies=tuple(late),
            sent=sent,
            send_failed=send_failed,
            foreign_replies=foreign,
        )
