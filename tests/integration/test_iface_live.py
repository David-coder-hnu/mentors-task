"""`RawIcmp6Iface` 对着真靶场的实弹测试 —— T2 的高风险点验证。

**为什么这个文件非有不可**：T1 证明的是「`ping` 能通」，那是内核自己的
ICMPv6 实现。我们要的是**原始套接字**能通，两者的差别恰好落在最可能出错的地方：

  1. **校验和**。`ping` 由内核填校验和；我们从 `IPPROTO_ICMPV6` 的 raw socket
     发出时，校验和字段填的是 0，**指望内核替我们填**。这是 `iface.py` 模块
     docstring 里点名的待验证点——它错了的话，包能发出去、靶场也会回应，
     但我们这边什么都收不到，症状与「靶场没配这个前缀」完全一样。
  2. **hop limit 的辅助数据**。`ping` 打印 hop limit 是内核给的；我们要靠
     `IPV6_RECVHOPLIMIT` 的 cmsg 自己取，取不到就静默变成 None。

所以这里的断言不是「跑通了」，而是**逐条钉住 `iface.py` 对外承诺的每一个字段**。

跑法（`docker/` 下）：

    docker compose --profile harness run --rm harness

宿主上自动 skip（`conftest.py` 的 `requires_raw_icmp6`）。
"""

from __future__ import annotations

import ipaddress
import select
import struct
import time
from typing import NamedTuple

import pytest

# `tests/integration/` 没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录
# 放进 sys.path，所以 conftest 可直接按顶层模块导入。用 `from .conftest` 反而会因为
# 它不是包而失败。
from conftest import (
    HARNESS_IFACE,
    LIVE_PREFIX,
    SILENT_PREFIX,
    TARGET_IPV6,
    requires_raw_icmp6,
)
from wire import WireCapture, icmp6_checksum_is_valid

from src.adapter.iface import DEFAULT_PAYLOAD, RawIcmp6Iface, SendFailure
from src.adapter.types import ICMP6_ECHO_REPLY

pytestmark = requires_raw_icmp6

#: 靶场把回包的 hop limit 硬编码成 64（`punchingbag.cpp:86` 传给
#: `libnet_build_ipv6` 的第 5 个参数）。据此断言**回包确实来自靶场**
#: 而不是路径上某个真实路由器——真实路径上的 hop limit 会小于 64。
#: （`LIVE_PREFIX` 本身在集成测试里只作提示用，见 conftest。）
TARGET_HOP_LIMIT = 64

#: 探测的地址落在 `LIVE_PREFIX` 里；靶场会把 src 伪造成它（`punchingbag.cpp:87`）。
PROBE_ADDR = "2001:db8:dead::1"
SILENT_ADDR = "2001:db8:0bad::1"

#: 我们的标识符。取一个不常用的值，免得和在链路上飘的其他 ICMPv6 撞车。
IDENTIFIER = 0x4D54  # "MT"

#: 把「探的地址」和「conftest 里的前缀」钉在一起。分成两处写是必要的（一个给靶场
#: 的 config、一个给探测），所以这里断言它们没走散——否则 config 改了前缀而探测
#: 地址没改时，症状会是「靶场不回包」，排查方向全错。
assert ipaddress.ip_address(PROBE_ADDR) in ipaddress.ip_network(LIVE_PREFIX)
assert ipaddress.ip_address(SILENT_ADDR) in ipaddress.ip_network(SILENT_PREFIX)


class Got(NamedTuple):
    src: str
    payload: bytes
    hop_limit: int | None
    icmp_type: int


def _await_reply(
    iface: RawIcmp6Iface,
    *,
    sequence: int,
    timeout: float,
) -> Got | None:
    """等到匹配 (IDENTIFIER, sequence) 的 Echo Reply，或超时返回 None。

    **必须连 sequence 一起匹配。** 只匹配 identifier 的话，上一条测试的迟到回包
    会冒充下一条的答案——而「迟到」在这套装置里是常态（靶场的 pcap 批处理窗口
    让 T1 观测到 5–97ms 的抖动）。那种假通过会把一个真实的回归盖住。

    用 `select` 而不是忙等：这一层本来就是非阻塞的，用它自己的 fileno 最省事。
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        readable, _, _ = select.select([iface.fileno], [], [], remaining)
        if not readable:
            return None
        for got in iface.recv_available():
            msg = got.message
            if msg.is_echo_reply and msg.identifier == IDENTIFIER and msg.sequence == sequence:
                return Got(got.src, got.payload, got.hop_limit, msg.icmp_type)


def _probe_expecting_reply(iface: RawIcmp6Iface, dst: str, sequence: int) -> Got:
    """发一个包并断言收到回包；失败时给出可执行的排查顺序。

    Args:
        sequence: 每条测试用**互不相同**的值，理由见 `_await_reply`。
    """
    iface.send_echo(dst, hop_limit=64, identifier=IDENTIFIER, sequence=sequence)
    got = _await_reply(iface, sequence=sequence, timeout=3.0)
    assert got is not None, (
        f"往 {dst} 发的 Echo Request（seq={sequence}）没等到回包。\n"
        "先别怀疑靶场配置，按顺序查：\n"
        f"  1. `ip -6 route` 里有没有指向 {TARGET_IPV6} 的探测前缀路由"
        "（没有的话包根本发不出去，见 conftest._route）\n"
        "  2. 靶场容器日志有没有 `libnet_init() failed`\n"
        "  3. 以上都对 → 怀疑 iface.py 把校验和填 0 交给内核这条假设"
    )
    return got


@pytest.fixture(scope="module", autouse=True)
def warmup() -> None:
    """等靶场的 pcap 真正打开。

    `depends_on` 只保证容器**启动**，不保证 Punching Bag 已经 `pcap_open_live`。
    在那之前发的包会被直接丢掉——而丢掉的表现是「超时」，与「这个前缀不回应」
    无法区分。所以先对着已知会被回应的地址热身，把这段窗口消耗掉。

    `autouse` + `scope="module"`：每条测试都要它，但只需要付一次代价。
    """
    with RawIcmp6Iface() as it:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                it.send_echo(PROBE_ADDR, hop_limit=64, identifier=IDENTIFIER, sequence=0)
            except SendFailure as exc:
                pytest.fail(f"连包都发不出去，靶场根本没起来: {exc}")
            if _await_reply(it, sequence=0, timeout=0.5) is not None:
                return
        pytest.fail(
            "30 秒内没等到任何回包。排查顺序同上：先看路由，再看靶场日志，"
            "最后怀疑校验和假设"
        )


def test_raw_socket_gets_reply_from_configured_prefix(iface: RawIcmp6Iface) -> None:
    """核心断言：原始套接字发的 Echo Request，靶场按配置回了包。

    这一条同时验掉了校验和假设——校验和错了就收不到任何东西。
    """
    got = _probe_expecting_reply(iface, PROBE_ADDR, sequence=1)
    assert got.icmp_type == ICMP6_ECHO_REPLY


def test_reply_is_source_spoofed_to_the_probed_address(iface: RawIcmp6Iface) -> None:
    """Stage 1 的承重性质：回包的 src 是**我们探的地址**，不是靶机的地址。

    这正是 Punching Bag 作为靶场的要点——它让「这个地址活着」变成由靶场配置
    决定的真值，而不是链路拓扑的副产品。
    """
    got = _probe_expecting_reply(iface, PROBE_ADDR, sequence=2)
    assert got.src == PROBE_ADDR, f"src 应被伪造成 {PROBE_ADDR}，实际 {got.src}"


def test_reply_hop_limit_is_the_spoofed_value(iface: RawIcmp6Iface) -> None:
    """hop limit = 64 由靶场伪造，且**我们的 cmsg 通路确实取到了它**。

    取到 None 是 `IPV6_RECVHOPLIMIT` 没生效——一条独立于「能不能收到包」的
    失败模式，所以单独一条断言。
    """
    got = _probe_expecting_reply(iface, PROBE_ADDR, sequence=3)
    assert got.hop_limit == TARGET_HOP_LIMIT, (
        f"hop_limit 应为 {TARGET_HOP_LIMIT}（靶场硬编码），实际 {got.hop_limit}。"
        "None → IPV6_RECVHOPLIMIT 的辅助数据没取到；"
        "其他值 → 回包可能来自路径上别的东西，不是靶场"
    )


def test_payload_is_echoed_verbatim(iface: RawIcmp6Iface) -> None:
    """载荷原样回抄（`punchingbag.cpp:178`）——「回包确属本次探测」的第二重凭据。

    第一重是 id/seq，但它只有 16 位、且是我们自己选的；载荷是内容凭证。
    """
    got = _probe_expecting_reply(iface, PROBE_ADDR, sequence=4)
    assert got.payload == DEFAULT_PAYLOAD


def test_kernel_fills_a_correct_icmpv6_checksum(iface: RawIcmp6Iface) -> None:
    """校验和字段留 0 交给内核 —— **实测确认内核真的填了，而且填对了**。

    `iface.py` 的模块 docstring 把这条件名为「未经实测的待验证点」，这个测试就是
    它的落点。**它不能靠「收到回包」来验**：靶场是 libpcap 抓包、不校验校验和，
    校验和写错了靶场照样回应。所以必须把帧抓下来自己算（见 `wire.py` 的说明）。
    """
    sequence = 20
    with WireCapture(HARNESS_IFACE) as wire:
        iface.send_echo(PROBE_ADDR, hop_limit=64, identifier=IDENTIFIER, sequence=sequence)
        frame = wire.wait_for_echo(
            identifier=IDENTIFIER, sequence=sequence, timeout=3.0
        )

    assert frame is not None, (
        "没在链路上看到我们自己发出的 Echo Request。不是校验和的问题——"
        "是抓包这一侧没工作（AF_PACKET 权限、网卡名）"
    )
    assert frame.dst == PROBE_ADDR, "抓到的应该是我们发往探测地址的那个包"
    # 载荷原样在线上，顺带证明 wire.py 的偏移算法没有错位。
    assert frame.icmp6[8:] == DEFAULT_PAYLOAD

    checksum = struct.unpack("!H", frame.icmp6[2:4])[0]
    assert checksum != 0, (
        "校验和字段是 0 —— **内核没有替我们填**，`iface.py` 的核心假设不成立。"
        "此时包能发出去、靶场也会回应，但真实网络的接收方会静默丢弃。"
    )
    assert icmp6_checksum_is_valid(frame.src, frame.dst, frame.icmp6), (
        f"线上校验和 0x{checksum:04x} 算不对（伪首部+报文的反码和不是 0xFFFF）"
    )


def test_negative_control_prefix_never_replies(iface: RawIcmp6Iface) -> None:
    """负对照：rate=0.0 的前缀必须一个回包都没有。

    **它是这一组里唯一能证伪「配置真的生效」的**：没有它，
    `test_raw_socket_gets_reply_from_configured_prefix` 在靶场忽略配置、
    无脑回一切的情况下同样会通过。

    发 5 个包（而非 1 个）是有意的：靶场按 `hash(addr) % 1000 < rate*1000` 决定
    是否回应，万一实现有偏差，单包偶然通过的概率远大于连续 5 包。
    """
    for offset in range(5):
        iface.send_echo(
            SILENT_ADDR, hop_limit=64, identifier=IDENTIFIER, sequence=10 + offset
        )
        got = _await_reply(iface, sequence=10 + offset, timeout=0.6)
        assert got is None, (
            f"rate=0.0 的前缀 {SILENT_ADDR} 回了包（src={got.src}），靶场未遵守 config"
        )


def test_send_failure_is_an_exception_not_a_sentinel(iface: RawIcmp6Iface) -> None:
    """发包失败必须是**显式异常**，不得塌成「超时」。

    设计方案 `:338` / `:1111`：把「网卡没起来」记成「地址不存在」会让丢包对账
    得出方向相反的结论。这里验的是 `send_echo` 的包装层——任何 `OSError` 都转成
    `SendFailure` 并带上真实 errno，而不是返回 None / -1 之类被调用方当超时处理。

    触发方式是关掉底层套接字（EBADF）。**不用「发给无路由地址」触发**，因为
    compose 网络有默认路由，那样的 `sendto` 会成功返回，测试会假通过。
    """
    iface.close()
    with pytest.raises(SendFailure) as excinfo:
        iface.send_echo(
            PROBE_ADDR, hop_limit=64, identifier=IDENTIFIER, sequence=99
        )
    assert excinfo.value.errno != 0, "SendFailure 应带上真实 errno，便于定位是哪种失败"
    assert excinfo.value.message
