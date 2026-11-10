"""Stage 2 拓扑、评分与跨进程 Agent 接线。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.adapter.router_shim import RouterRule, RouterShim
from src.adapter.truth import Stage2Truth
from src.adapter.types import Response
from src.agent.decide import parse_action
from src.agent.ablation import OpaqueEncoding
from src.agent.strategies import run_a1, run_a2, run_a3
from src.experiment.decision_broker import DecisionBroker, DecisionBrokerError
from src.experiment.session import stride_sampler
from src.experiment.stage2 import ROUTER_BASE, load_stage2_context
from src.scoring.score import RunRecord
from src.scoring.stage2 import score_stage2

ROOT = Path(__file__).resolve().parents[2]


def test_stage2_context_and_shim_share_one_truth() -> None:
    context = load_stage2_context(ROOT / "config" / "stage2_config.json")
    assert len(context.prefixes) == 32
    assert len(context.truth.routers) == 71
    assert sum(fields["aliased"] for fields in context.truth.prefixes.values()) == 8
    observed = context.executor(seed=0).run(
        [stride_sampler(prefix, 1)[0] for prefix in context.prefixes]
    )
    assert observed.sent == 32
    assert {response.src for response in observed.responses if response.src} <= set(
        context.truth.routers
    )


def test_stage2_score_counts_entities_not_responses() -> None:
    truth = Stage2Truth(
        routers=frozenset({"2001:db8:ffff::1", "2001:db8:ffff::2"}),
        prefixes={"2001:db8:5::/64": {"aliased": False, "n_routers": 2}},
    )
    run = RunRecord(
        probes=(
            Response("received", "2001:db8:5::1", src="2001:db8:ffff::1", icmp_type=1),
            Response("received", "2001:db8:5::2", src="2001:db8:ffff::1", icmp_type=1),
            Response("received", "2001:db8:5::3", src="2001:db8:ffff::2", icmp_type=1),
        )
    )
    result = score_stage2(run, truth)
    assert result.true_routers == 2
    assert result.indirect_responses == 3
    assert result.repeated_router_responses == 1
    assert result.router_entropy_bits == pytest.approx(0.918295834)


class TestDecisionBroker:
    config = {
        "model": "fake",
        "model_version": "fake-1",
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 64,
        "call_date": "2026-11-10",
    }

    def test_cache_hit_does_not_call_completion_again(self) -> None:
        calls: list[str] = []
        from src.agent.cache import DecisionCache

        broker = DecisionBroker(
            config=self.config,
            seed=3,
            cache=DecisionCache(),
            completion=lambda prompt: calls.append(prompt) or '{"action":"stop","reason":"x"}',
        )
        kwargs = {
            "prompt": "prompt",
            "state": {"prefixes": ("2001:db8:5::/64",)},
            "tool_pool": {"sample": "sample"},
        }
        first = broker.decide(**kwargs)
        second = broker.decide(**kwargs)
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert calls == ["prompt"]

    def test_replay_miss_is_loud(self) -> None:
        from src.agent.cache import DecisionCache

        broker = DecisionBroker(
            config=self.config,
            seed=3,
            cache=DecisionCache(),
            completion=None,
        )
        with pytest.raises(DecisionBrokerError, match="不会偷偷联网"):
            broker.decide(
                prompt="prompt",
                state={"prefixes": ("2001:db8:5::/64",)},
                tool_pool={"sample": "sample"},
            )


def test_construct_schema_is_closed_and_blueprint_whitelisted() -> None:
    action = parse_action(
        '{"action":"construct","name":"balanced_probe_smoke",'
        '"blueprint":"balanced_probe","count":10,"reason":"test"}'
    )
    assert action.kind == "construct"
    assert action.count == 10
    with pytest.raises(Exception, match="blueprint"):
        parse_action(
            '{"action":"construct","name":"bad","blueprint":"run_python",'
            '"count":10,"reason":"test"}'
        )


def test_response_rate_and_seed_make_router_choice_reproducible() -> None:
    rule = RouterRule(
        "2001:db8:5::/64",
        ("2001:db8:ffff::1", "2001:db8:ffff::2"),
        response_rate=0.5,
    )
    a = RouterShim([rule], seed=7)
    b = RouterShim([rule], seed=7)
    target = "2001:db8:5::99"
    assert a.response_for(target) == b.response_for(target)
    assert ROUTER_BASE.version == 6


class _AgentSurface:
    def __init__(self, decisions: list[str]) -> None:
        self.prefixes = ("2001:db8:5::/64", "2001:db8:5:1::/64")
        self.epoch_size = 2
        self._budget = 4
        self._decisions = list(decisions)
        self.tool_pools: list[dict[str, str]] = []
        self.prompts: list[str] = []
        self.reports: list[tuple[str, bool]] = []

    @property
    def budget_left(self) -> int:
        return self._budget

    def sample(self, prefix: str, n: int) -> list[Response]:
        self._budget -= n
        return [
            Response("sent_no_reply", f"{prefix.split('/')[0].rstrip(':')}::{i + 1}")
            for i in range(n)
        ]

    def report(self, prefix: str, aliased: bool) -> None:
        self.reports.append((prefix, aliased))

    def decide(self, *, prompt: str, state, tool_pool) -> tuple[str, bool]:
        self.prompts.append(prompt)
        self.tool_pools.append(dict(tool_pool))
        return self._decisions.pop(0), False


def test_a1_spends_epochs_then_uses_the_shared_report_rule() -> None:
    surface = _AgentSurface(
        [
            '{"action":"probe","prefix":"2001:db8:5::/64","count":2,"reason":"a"}',
            '{"action":"probe","prefix":"2001:db8:5:1::/64","count":2,"reason":"b"}',
        ]
    )
    run_a1(surface)
    assert surface.budget_left == 0
    assert "construct" not in surface.tool_pools[0]
    assert dict(surface.reports) == {
        "2001:db8:5::/64": False,
        "2001:db8:5:1::/64": False,
    }


def test_a2_can_construct_the_balanced_primitive() -> None:
    surface = _AgentSurface(
        [
            '{"action":"construct","name":"balanced_smoke",'
            '"blueprint":"balanced_probe","count":2,"reason":"a"}',
            '{"action":"construct","name":"balanced_smoke_2",'
            '"blueprint":"balanced_probe","count":2,"reason":"b"}',
        ]
    )
    run_a2(surface)
    assert surface.budget_left == 0
    assert "construct" in surface.tool_pools[0]
    assert dict(surface.reports) == {
        "2001:db8:5::/64": False,
        "2001:db8:5:1::/64": False,
    }


def test_a3_uses_opaque_regions_and_has_no_ipv6_text_in_prompt() -> None:
    prefixes = ("2001:db8:5::/64", "2001:db8:5:1::/64")
    encoding = OpaqueEncoding(prefixes)
    token = encoding.encode_prefix("2001:db8:5::/64")
    surface = _AgentSurface(
        [
            json.dumps(
                {
                    "action": "probe",
                    "region": token,
                    "count": 2,
                    "reason": "observe",
                }
            ),
            json.dumps(
                {
                    "action": "construct",
                    "name": "balanced",
                    "blueprint": "balanced_probe",
                    "count": 2,
                    "reason": "observe more",
                }
            ),
        ]
    )
    run_a3(surface)
    assert surface.budget_left == 0
    assert "2001" not in surface.prompts[0]
    assert "ipv6" not in surface.prompts[0].lower()
    assert dict(surface.reports) == {
        "2001:db8:5::/64": False,
        "2001:db8:5:1::/64": False,
    }
