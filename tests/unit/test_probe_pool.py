"""`ProbePool` 的调度、超时与三态 —— 设计方案 `T2` 点名要覆盖的两条：
**发包失败** 与 **迟到回包**。

全部用注入的假时钟 + 假 iface：这两条路径若靠真实挂钟撞，测试要么慢
（真的 sleep 到超时）要么飘（迟到与否取决于机器负载）。

`ProbePool` 不碰真套接字，所以这些测试在任何平台都能跑，**进 hosted CI**。
"""

from __future__ import annotations

import pytest

from src.adapter.iface import DEFAULT_PAYLOAD, Icmp6Message, Received, SendFailure
from src.adapter.probe_pool import LateReply, ProbePool, ProbeTarget
from src.adapter.types import ICMP6_ECHO_REPLY


class FakeClock:
    """可以手摇的单调时钟。`sleep` 只推进时间，不真的睡。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeIface:
    """一个不发包的 `RawIcmp6Iface` 替身。

    `script` 按 `dst` 给出该目标的命运：
      * `("reply", delay)` —— 在发出后 `delay` 秒到达一个回包
      * `("silent",)`      —— 什么都不回（`script` 里没有的 dst 默认如此）
      * `("error", msg)`   —— `send_echo` 直接抛 `SendFailure`

    `outstanding` / `peak_outstanding` 用来验证并发窗口：发出 +1，回包被取走 -1。

    `capacity` 模拟**套接字接收队列的容量**（T2(b)）。设了它，`_inbox` 满了之后
    新到的回包被**尾丢弃**——真内核就是这样丢的，且丢得**不留痕**：不给
    `recv_available` 任何信号，也不给 `Response` 任何字段。这正是那个必须被
    测出来的失效模式：丢掉的那一帧，在进程里看起来就是「这个地址没回包」。
    """

    def __init__(
        self, clock: FakeClock, script: dict[str, tuple], *, capacity: int | None = None
    ) -> None:
        self.clock = clock
        self.script = script
        #: 接收队列容量（包数）。`None` = 无限，即不模拟队列溢出。
        self.capacity = capacity
        #: 被尾丢弃的回包数。**只有测试看得见它**——真实环境下没有任何计数器。
        self.dropped = 0
        #: 调用轨迹，用来断言「填窗途中确实收过包」。
        self.trace: list[str] = []
        self.sent: list[tuple[str, int, int]] = []
        self.outstanding = 0
        self.peak_outstanding = 0
        self._inbox: list[tuple[float, Received]] = []

    def send_echo(self, dst, *, hop_limit, identifier, sequence, payload=DEFAULT_PAYLOAD):
        rule = self.script.get(dst, ("silent",))
        if rule[0] == "error":
            raise SendFailure(101, rule[1])
        self.trace.append("send")
        self.sent.append((dst, identifier, sequence))
        self.outstanding += 1
        self.peak_outstanding = max(self.peak_outstanding, self.outstanding)
        if rule[0] == "reply":
            if self.capacity is not None and len(self._inbox) >= self.capacity:
                # **尾丢弃，且不留痕**：不抛、不记、不给回包任何标记。
                # `outstanding` 也就此不再归零——那个探测会一路走到超时，
                # 与「地址不存在」同一个下场。
                self.dropped += 1
            else:
                self._inbox.append(
                    (
                        self.clock.now + rule[1],
                        _reply(dst, identifier, sequence, self.clock.now + rule[1], payload),
                    )
                )
        return self.clock.now

    def recv_available(self) -> list[Received]:
        self.trace.append("recv")
        ready = [r for t, r in self._inbox if t <= self.clock.now]
        self._inbox = [(t, r) for t, r in self._inbox if t > self.clock.now]
        self.outstanding -= len(ready)
        return ready

    def inject(self, got: Received) -> None:
        """从外部塞一个回包（模拟外来流量）。"""
        self._inbox.append((0.0, got))


def _msg(identifier: int, sequence: int) -> Icmp6Message:
    return Icmp6Message(ICMP6_ECHO_REPLY, 0, identifier, sequence)


def _reply(
    dst: str,
    identifier: int,
    sequence: int,
    arrival: float,
    payload: bytes = DEFAULT_PAYLOAD,
) -> Received:
    return Received(
        src=dst,
        message=_msg(identifier, sequence),
        hop_limit=64,
        payload=payload,
        recv_monotonic=arrival,
    )


def make_pool(iface: FakeIface, clock: FakeClock, **kwargs) -> ProbePool:
    options = dict(max_in_flight=4, identifier=0x1234, timeout_s=1.0)
    options.update(kwargs)
    return ProbePool(iface, clock=clock.monotonic, sleep=clock.sleep, **options)  # type: ignore[arg-type]


# ── 必填参数（V6-9）──────────────────────────────────────────────────────────


class TestMandatoryPoolSize:
    """池大小没有默认值。漏传必须炸，不能静默取一个默认值。"""

    def test_missing_max_in_flight_is_a_type_error(self) -> None:
        clock = FakeClock()
        with pytest.raises(TypeError):
            ProbePool(FakeIface(clock, {}), identifier=1)  # type: ignore[call-arg]

    def test_missing_identifier_is_a_type_error(self) -> None:
        clock = FakeClock()
        with pytest.raises(TypeError):
            ProbePool(FakeIface(clock, {}), max_in_flight=4)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_pool_size_rejected(self, bad: int) -> None:
        clock = FakeClock()
        with pytest.raises(ValueError, match="max_in_flight 至少为 1"):
            make_pool(FakeIface(clock, {}), clock, max_in_flight=bad)

    def test_negative_late_window_rejected(self) -> None:
        clock = FakeClock()
        with pytest.raises(ValueError, match="late_window_s 不能为负"):
            make_pool(FakeIface(clock, {}), clock, late_window_s=-1.0)


# ── 三态 ────────────────────────────────────────────────────────────────────


def test_reply_becomes_received_with_rtt() -> None:
    clock = FakeClock()
    iface = FakeIface(clock, {"a": ("reply", 0.25)})
    run = make_pool(iface, clock).run([ProbeTarget("a")])

    assert len(run) == 1
    (r,) = run.responses
    assert r.status == "received"
    assert r.src == "a"
    assert r.hop_limit == 64
    assert r.rtt_ms == pytest.approx(250.0)
    assert run.sent == 1 and run.send_failed == 0


def test_silence_becomes_sent_no_reply() -> None:
    clock = FakeClock()
    run = make_pool(FakeIface(clock, {}), clock).run([ProbeTarget("a")])

    (r,) = run.responses
    assert r.status == "sent_no_reply"
    assert r.src is None and r.error is None
    assert run.sent == 1


def test_send_failure_does_not_collapse_into_timeout() -> None:
    """**这条是 `T2` Verify 的第一句。** 发包失败必须自成一态。"""
    clock = FakeClock()
    iface = FakeIface(clock, {"never": ("error", "Network is unreachable")})
    run = make_pool(iface, clock).run([ProbeTarget("never")])

    (r,) = run.responses
    assert r.status == "send_failed", "发包失败被写成了超时——对账会把两件事混为一谈"
    assert r.error == "Network is unreachable"
    assert run.send_failed == 1
    assert run.sent == 0, "没发出去的包不能计入 sent，否则「实收/已发」的分母就错了"
    assert iface.sent == [], "既然 send_echo 抛了，就不该有发送记录"


def test_three_states_coexist_in_one_run() -> None:
    clock = FakeClock()
    iface = FakeIface(
        clock,
        {"ok": ("reply", 0.1), "dead": ("silent",), "bad": ("error", "no route")},
    )
    run = make_pool(iface, clock).run(["ok", "dead", "bad"])

    assert run.by_status() == {"received": 1, "sent_no_reply": 1, "send_failed": 1}
    assert run.sent == 2 and run.send_failed == 1


# ── 迟到回包（T2 Verify 的第二句）───────────────────────────────────────────


def test_late_reply_is_recorded_and_does_not_flip_the_result() -> None:
    """迟到不得翻案，但必须留痕。

    翻案会让结果取决于机器负载：同一份 `(config, seed)` 重跑会得到不同的
    回包集合，直接违反判据 6(i) 的「逐次完全相同」。
    """
    clock = FakeClock()
    iface = FakeIface(clock, {"slow": ("reply", 1.5)})  # 超时 1.0s，1.5s 才回
    run = make_pool(iface, clock, timeout_s=1.0, late_window_s=1.0).run([ProbeTarget("slow")])

    (r,) = run.responses
    assert r.status == "sent_no_reply", "迟到回包翻案了——结果会随机器负载变化"
    assert len(run.late_replies) == 1
    late = run.late_replies[0]
    assert isinstance(late, LateReply)
    assert late.dst == "slow" and late.src == "slow"
    assert late.delay_ms == pytest.approx(1500.0)
    assert late.timeout_ms == pytest.approx(1000.0)


def test_reply_just_inside_the_timeout_is_not_late() -> None:
    """边界另一侧：没超时就不该有迟到记录。"""
    clock = FakeClock()
    iface = FakeIface(clock, {"quick": ("reply", 0.9)})
    run = make_pool(iface, clock, timeout_s=1.0, late_window_s=1.0).run(["quick"])

    assert run.responses[0].status == "received"
    assert run.late_replies == ()


def test_late_window_defaults_to_zero_so_production_does_not_linger() -> None:
    """默认不守候 —— 生产路径不为一份诊断证据多等。

    这一条同时也是「窗口没开时 `late_replies` 必然为空」的说明：池子在最后一个
    探测结算完就退出，比它更晚的回包根本没机会被看到。
    """
    clock = FakeClock()
    iface = FakeIface(clock, {"slow": ("reply", 1.5)})
    run = make_pool(iface, clock, timeout_s=1.0).run(["slow"])

    assert run.responses[0].status == "sent_no_reply"
    assert run.late_replies == ()
    # 时钟只推进到超时那一刻，没有多守
    assert clock.now == pytest.approx(1.0, abs=0.01)


# ── 并发窗口 ────────────────────────────────────────────────────────────────


class TestConcurrencyWindow:
    def test_in_flight_never_exceeds_the_pool_size(self) -> None:
        clock = FakeClock()
        targets = [f"t{i}" for i in range(20)]
        iface = FakeIface(clock, {t: ("reply", 0.5) for t in targets})
        run = make_pool(iface, clock, max_in_flight=3).run(targets)

        assert len(run) == 20 and run.received == 20
        assert iface.peak_outstanding <= 3, "在途数超过了池大小"
        assert iface.peak_outstanding == 3, "窗口没被用满——分波而不是滑窗，吞吐会被低估"

    def test_pool_size_one_serializes(self) -> None:
        clock = FakeClock()
        targets = [f"t{i}" for i in range(3)]
        iface = FakeIface(clock, {t: ("reply", 0.5) for t in targets})
        run = make_pool(iface, clock, max_in_flight=1).run(targets)

        assert iface.peak_outstanding == 1
        assert run.received == 3
        assert clock.now >= 1.5 - 1e-9, f"串行三轮应至少推进 1.5s，实际 {clock.now}"


# ── 填窗途中也要收包（T2(b) 实测逼出来的一条）─────────────────────────────────
#
# 这一组用 `capacity` 把**套接字接收队列有限**这件事搬进单测。原先的
# 「先把整窗发满、再去收」在 `pool ≥ 4096` 的实机上丢掉七成回包，而靶场的
# 三层账本与我们的对账**全部报干净**——丢掉的那一帧在进程里没有任何痕迹。
# 所以这条路径必须有测试，否则它只会在实机上以「地址不存在」的样子回来。


class TestDrainDuringFill:
    def test_replies_survive_a_window_much_wider_than_the_receive_queue(self) -> None:
        clock = FakeClock()
        targets = [f"t{i}" for i in range(16)]
        # 全部立刻回包；接收队列只装得下 2 个 —— 与 425984 B ÷ 60 B 帧同构，
        # 只是把数量级缩到单测跑得动的规模。
        iface = FakeIface(clock, {t: ("reply", 0.0) for t in targets}, capacity=2)
        run = make_pool(iface, clock, max_in_flight=16, drain_every_sends=2).run(targets)

        assert iface.dropped == 0, f"接收队列溢出，丢了 {iface.dropped} 个回包"
        assert run.received == 16
        assert run.no_reply == 0

    def test_collect_happens_before_the_window_is_full(self) -> None:
        """断言的是**顺序**，不是数量：第一次收必须在窗口填满之前发生。

        这是这条修复的最小可证伪形式——旧代码头 16 个动作全是 `send`。
        """
        clock = FakeClock()
        targets = [f"t{i}" for i in range(16)]
        iface = FakeIface(clock, {t: ("reply", 0.0) for t in targets})
        make_pool(iface, clock, max_in_flight=16, drain_every_sends=4).run(targets)

        head = iface.trace[:16]
        assert "recv" in head, f"窗口填满之前一次都没收过包：{head}"

    def test_a_cadence_wider_than_the_window_is_what_loses_them(self) -> None:
        """反向对照：把节拍拉宽到超过整窗，丢包原样回来。

        没有这一条，上面那条测试也可能是因为别的原因通过——它证明「边填边收」
        确实是**充分的**，这一条证明它也是**必要的**。
        """
        clock = FakeClock()
        targets = [f"t{i}" for i in range(16)]
        iface = FakeIface(clock, {t: ("reply", 0.0) for t in targets}, capacity=2)
        run = make_pool(iface, clock, max_in_flight=16, drain_every_sends=1 << 30).run(targets)

        assert iface.dropped > 0, "节拍拉宽后居然没丢——说明 capacity 没生效，这组测试是假的"
        assert run.received < 16


def test_responses_keep_input_order() -> None:
    """顺序即契约：上层靠位置把 Response 映射回自己的目标表。"""
    clock = FakeClock()
    targets = ["a", "b", "c", "d"]
    iface = FakeIface(
        clock,
        {"a": ("reply", 0.4), "c": ("reply", 0.1), "d": ("error", "x")},
    )
    run = make_pool(iface, clock).run(targets)

    assert [r.dst for r in run.responses] == targets
    assert [r.status for r in run.responses] == [
        "received",
        "sent_no_reply",
        "received",
        "send_failed",
    ]


def test_empty_target_list_is_a_no_op() -> None:
    clock = FakeClock()
    run = make_pool(FakeIface(clock, {}), clock).run([])
    assert len(run) == 0 and run.sent == 0
    assert clock.now == 0.0, "没有目标就不该推进时钟"


# ── 外来流量 ────────────────────────────────────────────────────────────────


def test_foreign_replies_are_counted_not_claimed() -> None:
    """别的 id / 载荷不符的回包不得被认领——它会伪装成「这个地址活了」。"""
    clock = FakeClock()
    iface = FakeIface(clock, {"a": ("silent",)})
    iface.inject(
        Received(
            src="a",
            message=_msg(0x9999, 0),
            hop_limit=64,
            payload=DEFAULT_PAYLOAD,
            recv_monotonic=0.0,
        )
    )
    iface.inject(
        Received(
            src="a",
            message=_msg(0x1234, 0),
            hop_limit=64,
            payload=b"someone-else",
            recv_monotonic=0.0,
        )
    )
    run = make_pool(iface, clock).run(["a"])

    assert run.responses[0].status == "sent_no_reply"
    assert run.foreign_replies == 2


def test_payload_check_can_be_turned_off_for_stage2() -> None:
    """Stage 2 的 `RouterShim` 若不回抄载荷，`verify_payload=False` 仍能认领。"""
    clock = FakeClock()
    iface = FakeIface(clock, {"a": ("silent",)})
    iface.inject(
        Received(
            src="a",
            message=_msg(0x1234, 0),
            hop_limit=64,
            payload=b"rewritten-by-shim",
            recv_monotonic=0.0,
        )
    )
    run = make_pool(iface, clock, verify_payload=False).run(["a"])

    assert run.responses[0].status == "received"


def test_sequence_numbers_are_unique_and_monotonic() -> None:
    """seq 撞车会让回包错配到别的目标上——那是静默的错误结果。"""
    clock = FakeClock()
    targets = [f"t{i}" for i in range(50)]
    iface = FakeIface(clock, {t: ("reply", 0.01) for t in targets})
    make_pool(iface, clock, max_in_flight=8).run(targets)

    seqs = [seq for _, _, seq in iface.sent]
    assert len(seqs) == len(set(seqs)), "seq 有重复"
    assert seqs == sorted(seqs) and len(seqs) == 50
    assert all(ident == 0x1234 for _, ident, _ in iface.sent)
