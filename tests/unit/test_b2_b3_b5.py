"""T6b：B2 / B3 / B5 的核心算法契约。"""

from __future__ import annotations

import ipaddress
import random

import pytest

from src.adapter.types import Response
from src.baselines import b2_response_density, b3_bit_reversal, b5_frp_aware
from src.baselines.b5_frp_aware import AliasVerdict

P48 = "2001:db8:a000::/48"
P49 = "2001:db8:b000::/48"
P64 = "2001:db8:c000::/64"


class Answer:
    __slots__ = ("status",)

    def __init__(self, status: str) -> None:
        self.status = status


class FakeSurface:
    def __init__(self, prefixes, budget, *, alias_prefixes=(), seeded=False):
        self.prefixes = tuple(prefixes)
        self.epoch_size = 20
        self._budget = budget
        self._alias_prefixes = set(alias_prefixes)
        self._seeded = seeded
        self.probes: list[str] = []
        self.samples: list[tuple[str, int]] = []
        self.reports: list[tuple[str, bool]] = []

    @property
    def budget_left(self):
        return self._budget

    def rng(self):
        return random.Random(17)

    def sample(self, prefix, n):
        if self._budget < n:
            raise AssertionError("预算越界")
        self._budget -= n
        self.samples.append((prefix, n))
        received = prefix in self._alias_prefixes or (self._seeded and prefix == P49)
        return [Answer("received" if received else "sent_no_reply") for _ in range(n)]

    def probe(self, dst, proto, hop_limit=64):
        if self._budget < 1:
            raise AssertionError("预算越界")
        self._budget -= 1
        self.probes.append(dst)
        matched = next(
            prefix
            for prefix in sorted(
                self.prefixes, key=lambda p: ipaddress.ip_network(p).prefixlen, reverse=True
            )
            if ipaddress.ip_address(dst) in ipaddress.ip_network(prefix)
        )
        received = matched in self._alias_prefixes or (self._seeded and matched == P49)
        return Answer("received" if received else "sent_no_reply")

    def report(self, prefix, aliased):
        self.reports.append((prefix, aliased))


class TestB2:
    def test_unvisited_arms_are_covered_first(self) -> None:
        plan = b2_response_density.plan_round([0, 0, 0], [0, 0, 0], 3, random.Random(0))
        assert plan == (1, 1, 1)

    def test_highest_density_takes_the_round(self) -> None:
        plan = b2_response_density.plan_round(
            [10, 10, 10], [10.0, 1.0, 0.0], 20, random.Random(0)
        )
        assert plan[0] > sum(plan[1:])

    def test_run_spends_whole_budget_and_reports(self) -> None:
        surface = FakeSurface([P48, P49], 100, alias_prefixes={P48})
        b2_response_density.run(surface)
        assert surface.budget_left == 0
        assert sum(n for _, n in surface.samples) == 100
        assert len(surface.reports) == 2


class TestB3:
    def test_bit_reversal_order(self) -> None:
        assert [b3_bit_reversal.reverse_bits(i, 3) for i in range(8)] == [
            0,
            4,
            2,
            6,
            1,
            5,
            3,
            7,
        ]

    def test_addresses_are_unique_and_in_prefix(self) -> None:
        addresses = {
            b3_bit_reversal.address_at(P64, index)
            for index in range(100)
        }
        assert len(addresses) == 100
        assert all(
            ipaddress.ip_address(address) in ipaddress.ip_network(P64)
            for address in addresses
        )

    def test_round_robin_across_prefixes(self) -> None:
        targets = b3_bit_reversal.plan_targets([P48, P49], 6)
        assert [prefix for prefix, _ in targets] == [P48, P49, P48, P49, P48, P49]

    def test_run_spends_budget(self) -> None:
        surface = FakeSurface([P48, P49], 100)
        b3_bit_reversal.run(surface)
        assert surface.budget_left == 0
        assert len(surface.probes) == 100


class TestB5:
    def test_lengths_are_nested_under_task_prefix(self) -> None:
        assert b5_frp_aware.effective_lengths(P48) == (48, 64, 96, 112, 116, 120)
        assert b5_frp_aware.effective_lengths(P64) == (64, 96, 112, 116, 120)

    def test_three_unique_targets_at_requested_length(self) -> None:
        targets = b5_frp_aware.targets_for_length(P48, 112, random.Random(7))
        assert len(set(targets)) == 3
        assert all(ipaddress.ip_address(t).compressed for t in targets)
        first = ipaddress.ip_network(f"{targets[0]}/112", strict=False)
        assert first.subnet_of(ipaddress.ip_network(P48))

    def test_two_replies_declare_alias_early(self) -> None:
        class Surface(FakeSurface):
            def probe(self, dst, proto, hop_limit=64):
                self._budget -= 1
                self.probes.append(dst)
                return Answer("received" if len(self.probes) <= 2 else "sent_no_reply")

        surface = Surface([P48], 20)
        verdict = b5_frp_aware.detect_prefix(surface, P48, random.Random(1))
        assert verdict.aliased is True
        assert verdict.probes == 3
        assert verdict.completed_lengths == (48,)

    def test_negative_needs_all_six_groups(self) -> None:
        surface = FakeSurface([P48], 20)
        verdict = b5_frp_aware.detect_prefix(surface, P48, random.Random(1))
        assert verdict.aliased is False
        assert verdict.probes == 18
        assert verdict.completed_lengths == (48, 64, 96, 112, 116, 120)

    def test_partial_budget_returns_no_verdict(self) -> None:
        surface = FakeSurface([P48], 2)
        verdict = b5_frp_aware.detect_prefix(surface, P48, random.Random(1))
        assert verdict.aliased is None
        assert verdict.probes == 0

    def test_run_uses_detection_then_discovery(self) -> None:
        surface = FakeSurface([P48], 100, alias_prefixes={P48})
        b5_frp_aware.run(surface)
        # 3 个检测探针命中 aliased，剩余 97 探进入发现阶段。
        assert surface.budget_left == 0
        assert len(surface.probes) == 100
        assert surface.reports == [(P48, True)]
