"""Small-sample paired Wilcoxon power simulation."""

from __future__ import annotations

import random

from .stats import paired_wilcoxon


def paired_normal_power(
    *,
    effect: float,
    paired_sd: float,
    n: int,
    alpha: float = 0.05,
    simulations: int = 4000,
    seed: int = 0,
) -> float:
    """Estimate two-sided paired Wilcoxon power under normal paired differences."""

    if effect == 0:
        raise ValueError("effect 不能为 0")
    if paired_sd <= 0:
        raise ValueError("paired_sd 必须为正数")
    if n < 2:
        raise ValueError("n 至少为 2")
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 (0, 1)")
    if simulations < 1:
        raise ValueError("simulations 至少为 1")

    rng = random.Random(seed)
    rejected = 0
    for _ in range(simulations):
        differences = [effect + rng.gauss(0.0, paired_sd) for _ in range(n)]
        if paired_wilcoxon(differences, [0.0] * n).p_value < alpha:
            rejected += 1
    return rejected / simulations


__all__ = ["paired_normal_power"]
