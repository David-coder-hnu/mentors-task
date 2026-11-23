"""Runtime-composed primitive program validation and allocation."""

from __future__ import annotations

import pytest

from src.agent.primitive import PrimitiveError, compose_counts, parse_steps

PREFIXES = ("p0", "p1", "p2")


def test_balanced_step_exactly_spends_the_epoch() -> None:
    steps = parse_steps([{"target": "balanced", "fraction": 1.0}])
    counts = compose_counts(
        prefixes=PREFIXES,
        pulls={prefix: 0 for prefix in PREFIXES},
        replies={prefix: 0 for prefix in PREFIXES},
        steps=steps,
        count=5,
    )
    assert counts == (2, 2, 1)
    assert sum(counts) == 5


def test_ordered_program_prefers_unprobed_then_responsive() -> None:
    steps = parse_steps(
        [
            {"target": "unprobed", "fraction": 0.5},
            {"target": "responsive", "fraction": 0.5},
        ]
    )
    counts = compose_counts(
        prefixes=PREFIXES,
        pulls={"p0": 4, "p1": 0, "p2": 4},
        replies={"p0": 4, "p1": 0, "p2": 0},
        steps=steps,
        count=4,
    )
    assert counts[1] == 2
    assert counts[0] + counts[2] == 2
    assert sum(counts) == 4


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"target": "unknown", "fraction": 1.0}],
        [{"target": "balanced", "fraction": 0.4}],
        [{"target": "balanced", "fraction": 1.2}],
        [{"target": "balanced", "fraction": 1.0, "extra": True}],
    ],
)
def test_malformed_program_is_rejected(steps: list[dict[str, object]]) -> None:
    with pytest.raises(PrimitiveError):
        parse_steps(steps)
