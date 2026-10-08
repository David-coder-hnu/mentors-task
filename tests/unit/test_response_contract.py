"""`Response` 三态的不变量 —— 它是「发包失败不得塌进超时」这条要求的落点。

设计方案 `:338`：原文用 `src=None` 表示无响应、又另设 `timeout: bool`，
同一件事编码两次，**而「发包失败」无处安放**。塌成同一个 `timeout=True`
会让丢包对账把「网卡没起来」记成「地址不存在」。
"""

from __future__ import annotations

import pytest

from src.adapter.types import Response


def test_received_requires_src() -> None:
    with pytest.raises(ValueError, match="received 必须有 src"):
        Response(status="received", dst="2001:db8::1")


def test_received_must_not_carry_error() -> None:
    with pytest.raises(ValueError, match="received 不得带 error"):
        Response(status="received", dst="2001:db8::1", src="2001:db8::1", error="boom")


def test_send_failed_requires_error() -> None:
    with pytest.raises(ValueError, match="send_failed 必须带 error"):
        Response(status="send_failed", dst="2001:db8::1")


def test_send_failed_must_not_carry_src() -> None:
    with pytest.raises(ValueError, match="send_failed 不得有 src"):
        Response(status="send_failed", dst="2001:db8::1", src="2001:db8::1", error="boom")


def test_sent_no_reply_is_bare() -> None:
    with pytest.raises(ValueError, match="sent_no_reply 既无 src 也无 error"):
        Response(status="sent_no_reply", dst="2001:db8::1", src="2001:db8::1")


def test_valid_three_states_construct() -> None:
    assert Response(status="sent_no_reply", dst="2001:db8::1").src is None
    assert Response(status="send_failed", dst="2001:db8::1", error="ENETUNREACH").error
    ok = Response(status="received", dst="2001:db8::1", src="2001:db8::1", hop_limit=64)
    assert ok.hop_limit == 64


class TestIndirect:
    """`indirect` 是派生属性，不是可独立赋值的字段。"""

    def test_stage1_spoofed_source_is_not_indirect(self) -> None:
        """Stage 1 下 Punching Bag 伪造 src = 我们探的地址，所以 src == dst。"""
        r = Response(status="received", dst="2001:db8:dead::1", src="2001:db8:dead::1")
        assert not r.indirect

    def test_different_source_is_indirect(self) -> None:
        """Stage 2 的 RouterShim 合成最后一跳时，src 与 dst 分离（T15）。"""
        r = Response(status="received", dst="2001:db8:dead::1", src="2001:db8:dead::ff")
        assert r.indirect

    def test_no_reply_is_never_indirect(self) -> None:
        assert not Response(status="sent_no_reply", dst="2001:db8::1").indirect
        assert not Response(status="send_failed", dst="2001:db8::1", error="x").indirect
