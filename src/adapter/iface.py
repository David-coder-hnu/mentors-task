"""原始 ICMPv6 探测 I/O —— 靶场适配层的收发底座（T2）。

**这一层必须跑在 Linux 上**：它用 `AF_INET6/SOCK_RAW/IPPROTO_ICMPV6`，需要
`CAP_NET_RAW`。Windows 宿主做不到——T1 的 compose 里 `prober` 那个容器就是
为它准备的（两个容器 = 两个 netns，见 `docker/compose.yml`）。

**为什么手写而不是用 scapy**：设计方案要求发包是**确定性**的（同一个
`(config, seed)` 下重跑，靶场日志里被决定回应的包集合必须逐次完全相同，
`:693` 把这条定为进 CI 的精确相等断言）。scapy 的默认字段填充里有多处
随机量（id / 时间戳 / 选项顺序），要么逐个关掉，要么就把一个不确定源
引进了「可回放」这条硬约束的核心。stdlib 的 `socket` + `struct` 没有这个问题，
且不引第三方依赖。

**校验和：已实测确认**（2026-10-08，T2）。Linux 对 `IPPROTO_ICMPV6` 的 raw
socket 会替我们算校验和，所以我们把该字段填 0 交出去。这一点原先只是推断，
现由 `tests/integration/test_iface_live.py::test_kernel_fills_a_correct_icmpv6_checksum`
在链路上抓帧证实：字段被填成非 0，且伪首部+报文的反码和为 `0xFFFF`。

**这条性质无法靠「靶场回了包」来验**：Punching Bag 是 libpcap 抓包
（`pcap_open_live`），**它不校验校验和**——写错了照样回应。所以那条集成测试
必须自己抓帧算，工具在 `tests/integration/wire.py`。将来若有人把校验和改成
「自己填」，`test_checksum_field_is_left_zero_for_the_kernel` 会先炸。
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

from .types import (
    ICMP6_DEST_UNREACH,
    ICMP6_ECHO_REPLY,
    ICMP6_ECHO_REQUEST,
)

#: ICMPv6 Echo Request/Reply 的头部：type, code, checksum, identifier, sequence
_ECHO_HDR = struct.Struct("!BBHHH")

#: 默认载荷。Punching Bag 会把请求的载荷原样回抄（`punchingbag.cpp:64-74`），
#: 所以载荷本身可以作为「这个回包确实属于这次探测」的第二重凭据——id/seq 是
#: 第一重。
DEFAULT_PAYLOAD = b"mentors_task"


@dataclass(frozen=True, slots=True)
class Icmp6Message:
    """从裸字节解析出来的 ICMPv6 报文头。

    `identifier` / `sequence` **只对 Echo 类型有意义**：Destination Unreachable
    的头部布局不同（type, code, checksum, unused(4B)，之后是引发它的原包），
    那里我们把它们留成 None，而不是把 unused 字段硬读成 id/seq。
    """

    icmp_type: int
    icmp_code: int
    identifier: int | None
    sequence: int | None

    @property
    def is_echo_reply(self) -> bool:
        return self.icmp_type == ICMP6_ECHO_REPLY

    @property
    def is_unreachable(self) -> bool:
        return self.icmp_type == ICMP6_DEST_UNREACH


def build_echo_request(identifier: int, sequence: int, payload: bytes = DEFAULT_PAYLOAD) -> bytes:
    """构造一个 ICMPv6 Echo Request。

    校验和留 0 —— 由内核填。这是**已实测**的（见模块 docstring），改动它之前
    先读那一段：之所以不能自己填，是因为源地址由内核按路由决定，我们算不出
    伪首部。
    """
    if not 0 <= identifier <= 0xFFFF:
        raise ValueError(f"identifier 越界: {identifier}")
    if not 0 <= sequence <= 0xFFFF:
        raise ValueError(f"sequence 越界: {sequence}")
    return _ECHO_HDR.pack(ICMP6_ECHO_REQUEST, 0, 0, identifier, sequence) + payload


def parse_icmp6(data: bytes) -> Icmp6Message:
    """解析 ICMPv6 报文头。

    `data` 是**从 ICMPv6 头部开始**的字节——IPv6 raw socket 不把 IPv6 头交给
    应用层（RFC 3542 §3）。
    """
    if len(data) < _ECHO_HDR.size:
        raise ValueError(f"ICMPv6 报文太短: {len(data)} 字节")
    icmp_type, icmp_code, _checksum, field_a, field_b = _ECHO_HDR.unpack_from(data, 0)
    if icmp_type in (ICMP6_ECHO_REQUEST, ICMP6_ECHO_REPLY):
        return Icmp6Message(icmp_type, icmp_code, field_a, field_b)
    return Icmp6Message(icmp_type, icmp_code, None, None)


class SendFailure(Exception):
    """发包失败 —— 对应 `Response.status == "send_failed"`。

    **它必须是一个显式的异常路径，不得塌进超时**：设计方案 `:1111` 把
    「发包失败（无 CAP_NET_RAW / 无路由）」列为静默失败模式之一，而
    `:338` 说明了原因——塌成 `timeout=True` 会让丢包对账把「网卡没起来」
    记成「地址不存在」。
    """

    def __init__(self, errno: int, message: str) -> None:
        super().__init__(message)
        self.errno = errno
        self.message = message


@dataclass(frozen=True, slots=True)
class Received:
    """一次成功收到的回包（尚未匹配到具体探测）。"""

    src: str
    message: Icmp6Message
    hop_limit: int | None
    payload: bytes
    recv_monotonic: float


class RawIcmp6Iface:
    """一个原始 ICMPv6 套接字。

    非阻塞、可 `select`。它**不做** id/seq 匹配，也**不做**超时决策——
    那是 `probe_pool.py` 的事。这一层只回答两件事：
    包发出去了吗？收到了什么？
    """

    def __init__(self, recv_bufsize: int = 1 << 20) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
        except PermissionError as exc:  # pragma: no cover - 依赖容器能力
            raise SendFailure(1, f"无法创建原始套接字（缺 CAP_NET_RAW？）: {exc}") from exc

        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recv_bufsize)
        # 回包 hop limit 走辅助数据；没有这个 sockopt 就只能拿到 None。
        self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
        self._sock.setblocking(False)

    @property
    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def effective_recv_bufsize(self) -> int:
        """内核**实际**给的 `SO_RCVBUF`，不是我们请求的那个。

        必须能读到它：`setsockopt` 的请求值会被 `net.core.rmem_max` 悄悄截断，
        而截断的后果是**静默丢包**——回包进不了缓冲区，内核直接扔掉，我们在
        应用层看到的只是「没回包」，与「地址不存在」完全同形。
        请求 1 MiB 而内核只给 208 KiB 时，吞吐上界比预期低一个数量级，
        而现场没有任何一处会报错。
        """
        return int(self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> RawIcmp6Iface:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send_echo(
        self,
        dst: str,
        *,
        hop_limit: int,
        identifier: int,
        sequence: int,
        payload: bytes = DEFAULT_PAYLOAD,
    ) -> float:
        """发一个 Echo Request，返回发送时刻的单调时钟。

        Raises:
            SendFailure: 任何发包期错误。**不吞异常、不返回哨兵值。**
        """
        packet = build_echo_request(identifier, sequence, payload)
        try:
            self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, hop_limit)
            sent_at = time.monotonic()
            self._sock.sendto(packet, (dst, 0, 0, 0))
            return sent_at
        except OSError as exc:
            raise SendFailure(exc.errno or -1, f"发往 {dst} 失败: {exc.strerror or exc}") from exc

    def recv_available(self) -> list[Received]:
        """把套接字接收队列里**此刻所有**可读的回包取空。

        返回空列表 ≠ 超时：只表示这一瞬间没有。超时由调用方按挂钟判定。
        """
        out: list[Received] = []
        while True:
            try:
                data, ancdata, _flags, addr = self._sock.recvmsg(4096, 1024)
            except BlockingIOError:
                break
            except OSError:
                # 收到一个读不动的包（例如畸形）不该终止整轮探测。
                break

            hop_limit = None
            for level, ctype, cdata in ancdata:
                if level == socket.IPPROTO_IPV6 and ctype == socket.IPV6_HOPLIMIT:
                    hop_limit = struct.unpack("i", cdata[:4])[0]

            try:
                message = parse_icmp6(data)
            except ValueError:
                continue

            out.append(
                Received(
                    src=addr[0],
                    message=message,
                    hop_limit=hop_limit,
                    payload=data[_ECHO_HDR.size :],
                    recv_monotonic=time.monotonic(),
                )
            )
        return out
