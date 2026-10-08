"""`iface.py` 的纯编解码部分 —— 不碰网络，所以能在任何平台上跑（进 hosted CI）。

集成测试另见 `tests/integration/test_iface_live.py`（需要 CAP_NET_RAW）。
"""

from __future__ import annotations

import pytest

from src.adapter.iface import (
    DEFAULT_PAYLOAD,
    build_echo_request,
    parse_icmp6,
)
from src.adapter.types import (
    ICMP6_DEST_UNREACH,
    ICMP6_ECHO_REPLY,
    ICMP6_ECHO_REQUEST,
)


def test_echo_request_roundtrip() -> None:
    packet = build_echo_request(0x1234, 7, b"abc")
    msg = parse_icmp6(packet)
    assert msg.icmp_type == ICMP6_ECHO_REQUEST
    assert msg.icmp_code == 0
    assert msg.identifier == 0x1234
    assert msg.sequence == 7
    assert not msg.is_echo_reply


def test_checksum_field_is_left_zero_for_the_kernel() -> None:
    """校验和必须留 0 —— Linux 对 IPPROTO_ICMPV6 的 raw socket 会替我们算。

    这条不是风格问题：若我们**自己**算，就必须先知道内核选定的源地址，
    而源地址由内核按路由决定。所以只有「留 0 交给内核」这一个正确选项。
    """
    packet = build_echo_request(1, 1, b"")
    # type, code, checksum
    assert packet[2:4] == b"\x00\x00"


@pytest.mark.parametrize("identifier", [-1, 0x10000])
def test_identifier_out_of_range_rejected(identifier: int) -> None:
    with pytest.raises(ValueError, match="identifier 越界"):
        build_echo_request(identifier, 1)


@pytest.mark.parametrize("sequence", [-1, 0x10000])
def test_sequence_out_of_range_rejected(sequence: int) -> None:
    with pytest.raises(ValueError, match="sequence 越界"):
        build_echo_request(1, sequence)


def test_default_payload_is_stable() -> None:
    """默认载荷进包，且它是常量 —— 它兼作「回包确属本次探测」的第二重凭据。"""
    packet = build_echo_request(1, 1)
    assert packet.endswith(DEFAULT_PAYLOAD)


def test_parse_rejects_truncated_packet() -> None:
    with pytest.raises(ValueError, match="太短"):
        parse_icmp6(b"\x80\x00")


def test_parse_echo_reply() -> None:
    # 手工拼一个 Echo Reply：type=129, code=0, csum=0, id=0xBEEF, seq=3
    raw = bytes([ICMP6_ECHO_REPLY, 0, 0, 0]) + b"\xbe\xef\x00\x03" + b"payload"
    msg = parse_icmp6(raw)
    assert msg.is_echo_reply
    assert msg.identifier == 0xBEEF
    assert msg.sequence == 3


def test_destination_unreachable_has_no_id_seq() -> None:
    """`unreachable` 的头部布局不同（type, code, csum, unused(4B)）。

    把那 4 个 unused 字节硬读成 id/seq 会**静默**产出一对看起来合法的
    数字，然后拿去和探测表匹配。所以这里断言它们是 None。
    """
    raw = bytes([ICMP6_DEST_UNREACH, 0, 0, 0]) + b"\x00\x00\x00\x00" + b"\x00" * 40
    msg = parse_icmp6(raw)
    assert msg.is_unreachable
    assert msg.identifier is None
    assert msg.sequence is None
