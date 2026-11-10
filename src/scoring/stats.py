"""零依赖的配对检验与多重性校正（T11）。

主分析按 `(config, seed)` 配对，而不是按 state 配对。Stage 1 使用
Wilcoxon signed-rank：它只依赖差值符号与秩，适合样本量为 30 且不假设正态的
策略比较。

小样本（非零差值 `<= 16`）枚举全部符号翻转，得到精确双侧 p 值；更大样本用
平均秩、并列校正和连续性修正的正态近似。这样无需引入 SciPy/NumPy。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class StatsError(ValueError):
    """输入不满足配对检验契约。"""


@dataclass(frozen=True, slots=True)
class PairedTest:
    n_pairs: int
    n_nonzero: int
    mean_difference: float
    median_difference: float
    statistic: float
    p_value: float
    method: str
    rank_biserial: float


@dataclass(frozen=True, slots=True)
class HolmResult:
    name: str
    p_value: float
    adjusted_p: float
    rejected: bool


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        average = ((position + 1) + end) / 2.0
        for offset in order[position:end]:
            ranks[offset] = average
        position = end
    return ranks


def _exact_two_sided(ranks: Sequence[float], observed: float) -> float:
    count = len(ranks)
    threshold = abs(2.0 * observed - sum(ranks))
    tolerance = 1e-12
    extreme = 0
    total = 1 << count
    for mask in range(total):
        signed = sum(
            rank if mask & (1 << index) else -rank
            for index, rank in enumerate(ranks)
        )
        if abs(signed) >= threshold - tolerance:
            extreme += 1
    return extreme / total


def paired_wilcoxon(left: Sequence[float], right: Sequence[float]) -> PairedTest:
    """双侧 Wilcoxon signed-rank，`left - right` 为配对差值。"""

    if len(left) != len(right):
        raise StatsError(f"配对样本长度不同：{len(left)} != {len(right)}")
    if not left:
        raise StatsError("配对样本不能为空")
    for value in (*left, *right):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise StatsError(f"样本必须是数值，得到 {value!r}")
        if not math.isfinite(float(value)):
            raise StatsError(f"样本必须是有限数值，得到 {value!r}")

    differences = [float(a) - float(b) for a, b in zip(left, right, strict=True)]
    nonzero = [difference for difference in differences if difference != 0.0]
    mean_difference = statistics.fmean(differences)
    median_difference = statistics.median(differences)
    if not nonzero:
        return PairedTest(
            n_pairs=len(differences),
            n_nonzero=0,
            mean_difference=mean_difference,
            median_difference=median_difference,
            statistic=0.0,
            p_value=1.0,
            method="degenerate",
            rank_biserial=0.0,
        )

    ranks = _average_ranks([abs(value) for value in nonzero])
    w_plus = sum(rank for rank, difference in zip(ranks, nonzero) if difference > 0)
    w_minus = sum(rank for rank, difference in zip(ranks, nonzero) if difference < 0)

    if len(nonzero) <= 16:
        p_value = _exact_two_sided(ranks, w_plus)
        method = "exact"
    else:
        n = len(nonzero)
        mean = n * (n + 1) / 4.0
        variance = n * (n + 1) * (2 * n + 1) / 24.0
        # 并列值会降低秩和方差。按 `t^3 - t` 扣除。
        tie_counts: dict[float, int] = {}
        for rank in ranks:
            tie_counts[rank] = tie_counts.get(rank, 0) + 1
        variance -= sum(t**3 - t for t in tie_counts.values()) / 48.0
        if variance <= 0.0:
            p_value = 1.0 if abs(w_plus - w_minus) < 1e-12 else 0.0
        else:
            z = max(0.0, (abs(w_plus - mean) - 0.5) / math.sqrt(variance))
            p_value = math.erfc(z / math.sqrt(2.0))
        method = "normal"

    rank_total = w_plus + w_minus
    effect = 0.0 if rank_total == 0.0 else (w_plus - w_minus) / rank_total
    return PairedTest(
        n_pairs=len(differences),
        n_nonzero=len(nonzero),
        mean_difference=mean_difference,
        median_difference=median_difference,
        statistic=w_plus,
        p_value=min(1.0, max(0.0, p_value)),
        method=method,
        rank_biserial=effect,
    )


def holm_bonferroni(
    p_values: Mapping[str, float],
    *,
    alpha: float = 0.05,
) -> tuple[HolmResult, ...]:
    """Holm step-down 校正，保持输入名字、按校正后 p 升序输出。"""

    if not 0.0 < alpha < 1.0:
        raise StatsError(f"alpha 必须在 (0, 1)，得到 {alpha}")
    for name, p_value in p_values.items():
        if not math.isfinite(float(p_value)) or not 0.0 <= float(p_value) <= 1.0:
            raise StatsError(f"{name} 的 p 值不在 [0, 1]：{p_value!r}")

    ordered = sorted(p_values.items(), key=lambda item: item[1])
    count = len(ordered)
    adjusted_previous = 0.0
    rejected_previous = True
    out: list[HolmResult] = []
    for rank, (name, p_value) in enumerate(ordered):
        adjusted = min(1.0, (count - rank) * float(p_value))
        adjusted = max(adjusted, adjusted_previous)
        rejected = rejected_previous and adjusted <= alpha
        out.append(
            HolmResult(
                name=name,
                p_value=float(p_value),
                adjusted_p=adjusted,
                rejected=rejected,
            )
        )
        adjusted_previous = adjusted
        rejected_previous = rejected
    return tuple(out)


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    resamples: int = 5_000,
    seed: int = 0,
) -> tuple[float, float]:
    """确定 seed 的 percentile bootstrap 均值区间。"""

    if not values:
        raise StatsError("不能对空样本求区间")
    if not 0.0 < confidence < 1.0:
        raise StatsError(f"confidence 必须在 (0, 1)，得到 {confidence}")
    if resamples < 1:
        raise StatsError("resamples 至少为 1")
    import random

    rng = random.Random(seed)
    n = len(values)
    means = [
        statistics.fmean(values[int(rng.random() * n)] for _ in range(n))
        for _ in range(resamples)
    ]
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low_index = max(0, int(tail * (resamples - 1)))
    high_index = min(resamples - 1, int((1.0 - tail) * (resamples - 1)))
    return means[low_index], means[high_index]


__all__ = [
    "HolmResult",
    "PairedTest",
    "StatsError",
    "bootstrap_mean_ci",
    "holm_bonferroni",
    "paired_wilcoxon",
]
