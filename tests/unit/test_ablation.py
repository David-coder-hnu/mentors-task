"""T16：不透明同构编码只移除领域知识，不改变实验能力。"""

from __future__ import annotations

import pytest

from src.adapter.types import Response
from src.agent.ablation import AblationError, OpaqueEncoding, same_experiment_fields
from src.agent.decide import Action, Stage1State

P1 = "2001:db8:1::/64"
P2 = "2001:db8:2::/64"


def encoding() -> OpaqueEncoding:
    return OpaqueEncoding([P1, P2], salt="test")


class TestEncoding:
    def test_tokens_are_stable_and_contain_no_address_text(self) -> None:
        a = encoding()
        b = encoding()
        token = a.encode_prefix(P1)
        assert token == b.encode_prefix(P1)
        assert token.startswith("region-")
        assert "2001" not in token and "db8" not in token

    def test_roundtrip_prefix_and_address(self) -> None:
        enc = encoding()
        assert enc.decode_prefix(enc.encode_prefix(P1)) == P1
        assert enc.decode_address(enc.encode_address("2001:db8:1::1")) == "2001:db8:1::1"

    def test_response_encoding_removes_both_addresses(self) -> None:
        enc = encoding()
        response = Response(
            status="received",
            dst="2001:db8:1::1",
            src="2001:db8:9::1",
            icmp_type=1,
            icmp_code=0,
            hop_limit=32,
        )
        encoded = enc.encode_response(response)
        assert "2001" not in encoded.dst and "2001" not in str(encoded.src)
        assert encoded.hop_limit == 32

    def test_action_payload_translates_back_to_real_prefix(self) -> None:
        enc = encoding()
        token = enc.encode_prefix(P1)
        probe = enc.translate_action_payload(
            {"action": "probe", "region": token, "count": 3, "reason": "next"}
        )
        report = enc.translate_action_payload(
            {"action": "report", "region": token, "saturated": True, "reason": "flat"}
        )
        assert probe == Action(kind="probe", reason="next", prefix=P1, count=3)
        assert report == Action(kind="report", reason="flat", prefix=P1, aliased=True)

    def test_unknown_token_is_refused(self) -> None:
        with pytest.raises(AblationError):
            encoding().decode_prefix("region-deadbeef")


class TestPrompt:
    def test_prompt_has_no_ipv6_syntax_or_domain_terms(self) -> None:
        enc = encoding()
        state = Stage1State.initial(prefixes=[P1, P2], budget=10)
        prompt = enc.render_prompt(
            tools=(("probe", "probe a region"), ("report", "declare saturation")),
            state=state,
        )
        lowered = prompt.lower()
        for forbidden in ("ipv6", "2001:", "db8", "/64", "prefix"):
            assert forbidden not in lowered

    def test_state_tokens_roundtrip_to_the_same_real_state(self) -> None:
        enc = encoding()
        state = Stage1State.initial(prefixes=[P1, P2], budget=10)
        text = enc.render_state(state)
        assert enc.encode_prefix(P1) in text
        assert P1 not in text


class TestConfigAudit:
    def test_only_the_encoding_field_may_differ(self) -> None:
        base = {"model": "m", "temperature": 0, "budget": 100, "encoding": "raw"}
        ablated = {"model": "m", "temperature": 0, "budget": 100, "encoding": "opaque"}
        assert same_experiment_fields(base, ablated)
        assert not same_experiment_fields(base, {**ablated, "temperature": 1})
