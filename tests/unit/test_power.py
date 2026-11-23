"""Paired power sensitivity is executable and monotone."""

from __future__ import annotations

import pytest

from src.scoring.power import paired_normal_power


def test_larger_effects_have_more_power() -> None:
    small = paired_normal_power(
        effect=0.5, paired_sd=4.0, n=20, simulations=300, seed=7
    )
    large = paired_normal_power(
        effect=3.0, paired_sd=4.0, n=20, simulations=300, seed=7
    )
    assert large > small


@pytest.mark.parametrize(
    "kwargs",
    [
        {"effect": 0, "paired_sd": 1, "n": 10},
        {"effect": 1, "paired_sd": 0, "n": 10},
        {"effect": 1, "paired_sd": 1, "n": 1},
        {"effect": 1, "paired_sd": 1, "n": 10, "alpha": 0},
    ],
)
def test_invalid_power_inputs_are_rejected(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        paired_normal_power(simulations=10, **kwargs)
