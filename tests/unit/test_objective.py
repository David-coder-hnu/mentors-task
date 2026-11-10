"""T12：在线目标只依赖 Response，并对饱和来源降权。"""

from __future__ import annotations

import inspect
import math

import pytest

from src.adapter.types import Response
from src.agent.objective import (
    MIN_SATURATION_PROBES,
    ObjectiveError,
    OnlineObjective,
    SATURATION_REPLY_RATE,
)
from src.agent.prompts import render

P1 = "2001:db8:1::/64"
P2 = "2001:db8:2::/64"


def reply(dst: str, src: str | None = None, *, indirect: bool = False) -> Response:
    return Response(
        status="received",
        dst=dst,
        src=src or dst,
        icmp_type=1 if indirect else 129,
        icmp_code=0,
        hop_limit=32 if indirect else 64,
    )


def quiet(dst: str) -> Response:
    return Response(status="sent_no_reply", dst=dst)


def objective() -> OnlineObjective:
    return OnlineObjective([P1, P2])


class TestInputBoundary:
    def test_observe_only_accepts_response(self) -> None:
        with pytest.raises(ObjectiveError, match="只接受 Response"):
            objective().observe({"status": "received"})  # type: ignore[arg-type]

    def test_the_module_does_not_import_truth_or_scoring_layers(self) -> None:
        import src.agent.objective as module

        source = inspect.getsource(module)
        assert "ground_truth" not in source
        assert "Stage1Truth" not in source
        assert "src.scoring" not in source

    def test_out_of_scope_response_is_refused(self) -> None:
        with pytest.raises(ObjectiveError, match="不在任务前缀内"):
            objective().observe(reply("2001:db8:ff::1"))


class TestSignal:
    def test_all_aliased_style_replies_saturate_to_zero_value(self) -> None:
        state = objective()
        for i in range(MIN_SATURATION_PROBES):
            state = state.observe(reply(f"2001:db8:1::{i + 1:x}"))
        snapshot = state.snapshot()
        assert snapshot.saturated_prefixes == (P1,)
        assert snapshot.effective_signatures == 0.0
        assert snapshot.online_info_bits == 0.0
        assert snapshot.duplicate_reply_rate > 0.7

    def test_a_sparse_credible_prefix_keeps_value(self) -> None:
        state = objective()
        for i in range(MIN_SATURATION_PROBES):
            state = state.observe(reply(f"2001:db8:1::{i + 1:x}") if i < 2 else quiet(f"2001:db8:1::{i + 1:x}"))
        snapshot = state.snapshot()
        assert snapshot.saturated_prefixes == ()
        assert snapshot.credible_prefixes == (P1,)
        assert snapshot.effective_signatures == 1.0

    def test_distinct_indirect_sources_increase_value(self) -> None:
        state = objective()
        for i, src in enumerate(("2001:db8:9::1", "2001:db8:9::2", "2001:db8:9::3")):
            state = state.observe(reply(f"2001:db8:1::{i + 1:x}", src, indirect=True))
        first = state.snapshot()
        assert first.unique_signatures == 3
        assert first.effective_signatures == pytest.approx(3.0)

    def test_repeating_a_source_does_not_increase_value(self) -> None:
        state = objective()
        src = "2001:db8:9::1"
        for _ in range(3):
            state = state.observe(reply("2001:db8:1::1", src, indirect=True))
        snapshot = state.snapshot()
        assert snapshot.unique_signatures == 1
        assert snapshot.effective_signatures == pytest.approx(1.0)
        assert snapshot.online_info_bits == 0.0

    def test_order_independence(self) -> None:
        responses = [
            reply("2001:db8:1::1", "2001:db8:9::1", indirect=True),
            quiet("2001:db8:1::2"),
            reply("2001:db8:2::1"),
            reply("2001:db8:2::2"),
        ]
        a = objective().observe_many(responses)
        b = objective().observe_many(reversed(responses))
        assert a.snapshot() == b.snapshot()

    def test_thresholds_are_explicit(self) -> None:
        assert MIN_SATURATION_PROBES == 5
        assert SATURATION_REPLY_RATE == 0.8


class TestPrompt:
    def test_objective_guidance_is_appended_without_truth_claim(self) -> None:
        state = objective().observe(reply("2001:db8:1::1"))
        prompt = render(tools=(), state_text="state", objective_text=state.render())
        assert "## 在线目标" in prompt
        assert "不是真值" in prompt
        assert "在线价值" in prompt
