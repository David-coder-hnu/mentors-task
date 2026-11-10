"""T15：RouterShim 的逐字段契约。"""

from __future__ import annotations

import pytest

from src.adapter.router_shim import RouterRule, RouterShim, RouterShimError
from src.adapter.types import ICMP6_DEST_UNREACH

P1 = "2001:db8:aa::/48"
P2 = "2001:db8:bb::/64"
ALIAS = "2001:db8:cc::/48"
R1 = "2001:db8:1::1"
R2 = "2001:db8:1::2"
RA = "2001:db8:ca::1"


def shim() -> RouterShim:
    return RouterShim(
        [
            RouterRule(P1, (R1, R2), hop_limit=7),
            RouterRule(P2, (R2,), hop_limit=9),
            RouterRule(ALIAS, (RA,), aliased=True, hop_limit=5),
        ]
    )


class TestResponseContract:
    def test_an_unreachable_address_gets_an_indirect_icmp_response(self) -> None:
        response = shim().response_for("2001:db8:aa:ffff::42")
        assert response.status == "received"
        assert response.dst == "2001:db8:aa:ffff::42"
        assert response.src in {R1, R2}
        assert response.src != response.dst
        assert response.indirect is True
        assert response.icmp_type == ICMP6_DEST_UNREACH
        assert response.icmp_code == 0
        assert response.hop_limit == 7
        assert response.rtt_ms == 0.0

    def test_longest_prefix_wins(self) -> None:
        assert shim().response_for("2001:db8:bb::1").src == R2
        assert shim().response_for("2001:db8:bb::1").hop_limit == 9

    def test_an_aliased_prefix_exposes_one_source(self) -> None:
        s = shim()
        sources = {
            s.response_for(f"2001:db8:cc:{i:x}::1").src
            for i in range(16)
        }
        assert sources == {RA}

    def test_out_of_range_is_a_real_non_reply(self) -> None:
        response = shim().response_for("2001:db8:ff::1")
        assert response.status == "sent_no_reply"
        assert response.src is None


class TestTruthContract:
    def test_sources_equal_ground_truth_routers(self) -> None:
        s = shim()
        targets = [
            "2001:db8:aa::1",
            "2001:db8:aa:1::2",
            "2001:db8:bb::3",
            "2001:db8:cc::4",
        ]
        run = s.run(targets)
        observed = {r.src for r in run.responses if r.src is not None}
        assert observed == set(s.ground_truth().routers)

    def test_topology_and_prefix_counts_are_consistent(self) -> None:
        truth = shim().ground_truth()
        assert truth.topology["2001:db8:aa::/48"] == (R1, R2)
        assert truth.prefixes["2001:db8:aa::/48"]["n_routers"] == 2
        assert truth.prefixes["2001:db8:cc::/48"] == {"aliased": True, "n_routers": 1}

    def test_deterministic_router_choice(self) -> None:
        a = shim().response_for("2001:db8:aa::55")
        b = shim().response_for("2001:db8:aa::55")
        assert a == b


class TestValidation:
    def test_aliased_rule_cannot_hide_multiple_sources(self) -> None:
        with pytest.raises(RouterShimError, match="只暴露一个来源"):
            RouterRule(ALIAS, (R1, R2), aliased=True)

    def test_duplicate_prefix_is_refused(self) -> None:
        with pytest.raises(RouterShimError, match="重复"):
            RouterShim([RouterRule(P1, (R1,)), RouterRule(P1, (R2,))])

    @pytest.mark.parametrize("hop", [0, 256])
    def test_invalid_hop_limit_is_refused(self, hop: int) -> None:
        with pytest.raises(RouterShimError, match="hop_limit"):
            RouterRule(P1, (R1,), hop_limit=hop)
