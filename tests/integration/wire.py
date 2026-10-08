"""在链路上抓自己发出去的包，用来验证**线上真实字节**。

**为什么非要有这一层**：探测层里最容易出错的地方是「包能不能收到回复」，
但有些性质**收到回复也证明不了**。最典型的就是 ICMPv6 校验和——

  * 靶场用 libpcap 抓包（`pcap_open_live`），**它不校验校验和**。所以哪怕我们
    发出去的包校验和是错的，靶场照样会回应。「收到回包」证明不了校验和正确。
  * 也不能改成「发给靶机自己的地址、看内核回不回」：容器网络是 veth 全虚拟链路，
    接收侧常被标成 `CHECKSUM_UNNECESSARY`，坏校验和会被跳过。

只剩一条路：**把帧在链路上抓下来，自己算一遍**。`AF_PACKET/SOCK_RAW` 就是
libpcap 底下那层，而且能看到**本机发出的**帧（用 `ETH_P_ALL`，见下）。

**为什么用 `ETH_P_ALL` 而不是 `ETH_P_IPV6`**：实测中限定 `ETH_P_IPV6` 时
抓不到本机发出的 Echo Request（收到的帧数也偏少）；`ETH_P_ALL` 稳定抓到。
与其去猜是哪个内核路径的差异，不如在用户态过一道过滤——反正量小。

这是**测试脚手架**，不是适配层的一部分。若将来 Oracle 侧需要在链路上取证，
再把它提到 `src/` 下（那时应当换成一个能写 pcap 文件的形态）。
"""

from __future__ import annotations

import socket
import struct
import time
from typing import NamedTuple

ETH_P_ALL = 0x0003
ETHERTYPE_IPV6 = b"\x86\xdd"

#: Ethernet 头 14 字节 + IPv6 定长头 40 字节。
_ICMP6_OFFSET = 14 + 40

_ETH_SRC = slice(6, 12)
_IPV6_PAYLOAD_LEN = slice(18, 20)  # IPv6 头内偏移 4，长度 2
_IPV6_SRC = slice(22, 38)
_IPV6_DST = slice(38, 54)
_IPV6_NEXT_HEADER = 20

IPPROTO_ICMPV6 = 58
ICMP6_ECHO_REQUEST = 128

#: 最小 Echo 头长度（type, code, checksum, id, seq）。
_ECHO_HDR_SIZE = 8


class CapturedFrame(NamedTuple):
    """链路层抓到的、已解析出 IPv6 + ICMPv6 的一帧。"""

    eth_src: str
    src: str
    dst: str
    icmp6: bytes
    recv_monotonic: float

    @property
    def echo_key(self) -> tuple[int, int] | None:
        """Echo 报文的 `(identifier, sequence)`；非 Echo 返回 None。"""
        if len(self.icmp6) < _ECHO_HDR_SIZE or self.icmp6[0] not in (128, 129):
            return None
        return struct.unpack("!HH", self.icmp6[4:8])


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


class WireCapture:
    """把接口上流过的帧捞出来。

    用 `AF_PACKET/SOCK_RAW` 而不是 `SOCK_DGRAM`：后者会剥掉以太网头，
    而我们要按下一跳 MAC 判断方向。
    """

    def __init__(self, interface: str = "eth0") -> None:
        self._interface = interface
        try:
            self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(ETH_P_ALL))
        except PermissionError as exc:  # pragma: no cover - 依赖容器能力
            raise RuntimeError(f"打不开 AF_PACKET（缺 CAP_NET_RAW？）: {exc}") from exc
        self._sock.bind((interface, 0))
        self._sock.setblocking(False)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> WireCapture:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def own_mac(self) -> str:
        """本机接口的 MAC —— 用它区分「我们发出的」与「我们收到的」。"""
        return open(f"/sys/class/net/{self._interface}/address").read().strip()

    def _drain(self) -> list[CapturedFrame]:
        """取空当前可读的帧，只留下能解析成 IPv6/ICMPv6 的。"""
        out: list[CapturedFrame] = []
        while True:
            try:
                frame = self._sock.recv(65535)
            except BlockingIOError:
                return out
            except OSError:
                return out

            now = time.monotonic()
            if len(frame) < _ICMP6_OFFSET or frame[12:14] != ETHERTYPE_IPV6:
                continue
            if frame[_IPV6_NEXT_HEADER] != IPPROTO_ICMPV6:
                continue
            # **必须按 IPv6 的 payload length 切片，不能一路切到帧尾。**
            # 以太网最短帧 60 字节，短包会被填充；切到帧尾就把填充字节也算进
            # 报文，而校验和是按长度算的——多一个字节就会得出「校验和错误」
            # 这个假结论。这里正好踩在边界上：20 字节的 ICMPv6 报文凑出 74 字节
            # 的帧，躲过了填充；换个短载荷就会中招。
            payload_len = struct.unpack("!H", frame[_IPV6_PAYLOAD_LEN])[0]
            end = _ICMP6_OFFSET + payload_len
            if len(frame) < end:
                continue  # 抓包被截断（snaplen），不是完整报文

            # IPv6 定长头就是 40 字节；本测试不构造扩展头，所以不做逐跳遍历。
            # 真出现扩展头时这里会解析出垃圾——但那本身就是值得知道的信号。
            out.append(
                CapturedFrame(
                    eth_src=_mac(frame[_ETH_SRC]),
                    src=socket.inet_ntop(socket.AF_INET6, frame[_IPV6_SRC]),
                    dst=socket.inet_ntop(socket.AF_INET6, frame[_IPV6_DST]),
                    icmp6=frame[_ICMP6_OFFSET:end],
                    recv_monotonic=now,
                )
            )

    def wait_for_echo(
        self,
        *,
        identifier: int,
        sequence: int,
        echo_type: int = ICMP6_ECHO_REQUEST,
        timeout: float = 2.0,
        outgoing_only: bool = False,
    ) -> CapturedFrame | None:
        """等一个指定 `(identifier, sequence)` 的 Echo 帧。

        Args:
            echo_type: 128 找我们发出去的请求，129 找回包。
            outgoing_only: 只认本机发出的。**默认不设**——因为 `AF_PACKET`
                抓到的是同一个 `(id, seq)` 的两个方向，而回包的 id/seq 由靶场
                原样回抄，两者会撞车。要区分只能靠方向或 ICMP type。
        """
        own_mac = self.own_mac if outgoing_only else None
        deadline = time.monotonic() + timeout
        while True:
            for frame in self._drain():
                if frame.echo_key != (identifier, sequence):
                    continue
                if frame.icmp6[0] != echo_type:
                    continue
                if outgoing_only and frame.eth_src != own_mac:
                    continue
                return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)


# ── 校验和 ──────────────────────────────────────────────────────────────────


def ones_complement_sum(data: bytes) -> int:
    """16 位反码和（RFC 1071）。`data` 长度为奇数时右补一个零字节。"""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return total


def icmp6_checksum_is_valid(src: str, dst: str, icmp6: bytes) -> bool:
    """按 RFC 4443 §2.3 校验 ICMPv6 校验和。

    校验和字段**是报文的一部分**，所以算对了的话整个串（伪首部 + 报文）的
    反码和是 `0xFFFF`——不需要先把字段清零再比对。
    """
    pseudo = (
        socket.inet_pton(socket.AF_INET6, src)
        + socket.inet_pton(socket.AF_INET6, dst)
        + struct.pack("!I", len(icmp6))
        + b"\x00\x00\x00"
        + bytes([IPPROTO_ICMPV6])
    )
    return ones_complement_sum(pseudo + icmp6) == 0xFFFF
