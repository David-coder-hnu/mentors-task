"""T18：留出集必须在拓扑、aliased 组成与响应率分布上独立于调参集。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import ipaddress
import pytest

from scripts.make_heldout import (
    DEFAULT_OUTPUT,
    DEFAULT_SEED,
    DEFAULT_TUNING_CONFIG,
    HELDOUT_ROUTE,
    HeldoutConfigError,
    build_heldout_config,
    render_config,
)
from src.adapter.ground_truth import read_subnets, stage1_truth_from_config


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _prefix_set(config: dict) -> set[str]:
    return {
        str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        for entry in config["subnets"]
    }


def _role_counts(config: dict) -> Counter[str]:
    return Counter(entry["_role"] for entry in config["subnets"])


def _rate_profile_counts(config: dict) -> Counter[tuple[str, tuple[float, ...]]]:
    return Counter(
        (
            entry["_role"],
            (
                entry["default_response_rate"],
                entry["EUI_response_rate"],
                entry["lower_response_rate"],
                entry["higher_response_rate"],
            ),
        )
        for entry in config["subnets"]
    )


@pytest.fixture(scope="module")
def tuning() -> dict:
    return _load(DEFAULT_TUNING_CONFIG)


@pytest.fixture(scope="module")
def heldout() -> dict:
    return _load(DEFAULT_OUTPUT)


class TestGeneratedFile:
    def test_the_committed_file_is_exactly_the_generator_output(self) -> None:
        expected = render_config(
            build_heldout_config(DEFAULT_TUNING_CONFIG, seed=DEFAULT_SEED)
        )
        assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == expected

    def test_the_same_seed_reproduces_the_same_bytes(self) -> None:
        first = render_config(build_heldout_config(seed=7))
        second = render_config(build_heldout_config(seed=7))
        assert first == second

    def test_a_different_seed_changes_order_but_not_composition(self) -> None:
        first = build_heldout_config(seed=7)
        second = build_heldout_config(seed=8)
        assert _prefix_set(first) == _prefix_set(second)
        assert [e["_role"] for e in first["subnets"]] != [
            e["_role"] for e in second["subnets"]
        ]
        assert _role_counts(first) == _role_counts(second)

    @pytest.mark.parametrize("seed", [-1, True, 1.5])
    def test_invalid_seeds_are_refused(self, seed) -> None:
        with pytest.raises(HeldoutConfigError):
            build_heldout_config(seed=seed)


class TestTheTopologyIsDisjoint:
    def test_no_prefix_is_shared_with_the_tuning_set(
        self, tuning: dict, heldout: dict
    ) -> None:
        assert _prefix_set(tuning).isdisjoint(_prefix_set(heldout))

    def test_every_heldout_prefix_lives_in_the_separate_route(
        self, heldout: dict
    ) -> None:
        prefixes = [
            ipaddress.ip_network(entry["ipv6_prefix"], strict=False)
            for entry in heldout["subnets"]
        ]
        assert len(prefixes) == 32
        assert all(prefix.prefixlen == 64 for prefix in prefixes)
        assert all(prefix.subnet_of(HELDOUT_ROUTE) for prefix in prefixes)

    def test_the_heldout_file_is_valid_ground_truth(self) -> None:
        table = read_subnets(DEFAULT_OUTPUT)
        truth = stage1_truth_from_config(DEFAULT_OUTPUT)
        assert table.skipped == ()
        assert table.conflicting == ()
        assert len(table.configured) == 32
        assert len(truth.prefixes) == 32
        assert len(table.aliased) == 6


class TestDistributionsShift:
    def test_the_aliased_composition_differs(
        self, tuning: dict, heldout: dict
    ) -> None:
        tuning_roles = _role_counts(tuning)
        heldout_roles = _role_counts(heldout)
        assert tuning_roles["aliased"] == 8
        assert heldout_roles["aliased"] == 6
        assert tuning_roles != heldout_roles
        assert heldout_roles == Counter(
            {
                "aliased": 6,
                "content-mid": 7,
                "content-low": 7,
                "content-sparse": 4,
                "tier-eui-hot": 2,
                "tier-byte-hot": 2,
                "silent": 4,
            }
        )

    def test_the_response_rate_profiles_differ(
        self, tuning: dict, heldout: dict
    ) -> None:
        tuning_profiles = _rate_profile_counts(tuning)
        heldout_profiles = _rate_profile_counts(heldout)
        assert tuning_profiles != heldout_profiles

        heldout_rates = {
            entry["_role"]: (
                entry["default_response_rate"],
                entry["EUI_response_rate"],
                entry["lower_response_rate"],
                entry["higher_response_rate"],
            )
            for entry in heldout["subnets"]
        }
        assert heldout_rates["content-mid"] == (0.35, 0.35, 0.35, 0.35)
        assert heldout_rates["content-low"] == (0.08, 0.08, 0.08, 0.08)
        assert heldout_rates["content-sparse"] == (0.01, 0.01, 0.01, 0.01)
        assert heldout_rates["tier-eui-hot"] == (0.01, 0.90, 0.01, 0.01)
        assert heldout_rates["tier-byte-hot"] == (0.01, 0.01, 0.65, 0.65)

    def test_only_the_all_ones_profile_is_aliased(self, heldout: dict) -> None:
        table = read_subnets(DEFAULT_OUTPUT)
        for prefix, rates in table.subnets.items():
            assert (prefix in table.aliased) == all(rate == 1.0 for rate in rates.all_rates)
