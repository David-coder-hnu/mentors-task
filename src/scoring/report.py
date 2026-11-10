"""逐 seed 分布与配对比较报告（T11）。

`plans/` 与论文口径都要求报每 seed 分布，不能只报均值：LLM 策略可能双峰，
均值会把“有时很好、有时失效”压成一个看似普通的数。
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .stats import (
    HolmResult,
    PairedTest,
    bootstrap_mean_ci,
    holm_bonferroni,
    paired_wilcoxon,
)


class ReportError(ValueError):
    """策略序列或比较定义不合法。"""


@dataclass(frozen=True, slots=True)
class Distribution:
    n: int
    mean: float
    median: float
    stdev: float
    minimum: float
    q25: float
    q75: float
    maximum: float
    ci_low: float
    ci_high: float


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    name: str
    left: str
    right: str
    test: PairedTest
    holm: HolmResult
    seeds: tuple[int, ...]


def summarize_distribution(values: Sequence[float], *, seed: int = 0) -> Distribution:
    if not values:
        raise ReportError("不能汇总空分布")
    numeric = [float(value) for value in values]
    if not all(abs(value) < float("inf") for value in numeric):
        raise ReportError("分布必须是有限数值")
    ci_low, ci_high = bootstrap_mean_ci(numeric, seed=seed)
    return Distribution(
        n=len(numeric),
        mean=statistics.fmean(numeric),
        median=statistics.median(numeric),
        stdev=statistics.stdev(numeric) if len(numeric) > 1 else 0.0,
        minimum=min(numeric),
        q25=statistics.quantiles(numeric, n=4, method="inclusive")[0]
        if len(numeric) > 1
        else numeric[0],
        q75=statistics.quantiles(numeric, n=4, method="inclusive")[2]
        if len(numeric) > 1
        else numeric[0],
        maximum=max(numeric),
        ci_low=ci_low,
        ci_high=ci_high,
    )


def _aligned(
    series: Mapping[str, Mapping[int, float]],
    left: str,
    right: str,
) -> tuple[tuple[int, ...], list[float], list[float]]:
    if left not in series or right not in series:
        missing = [name for name in (left, right) if name not in series]
        raise ReportError(f"缺少策略序列：{missing}")
    left_seeds = set(series[left])
    right_seeds = set(series[right])
    if left_seeds != right_seeds:
        raise ReportError(
            f"{left} 与 {right} 的 seed 集合不同："
            f"{sorted(left_seeds ^ right_seeds)}"
        )
    seeds = tuple(sorted(left_seeds))
    if not seeds:
        raise ReportError(f"{left} 与 {right} 没有 seed")
    return (
        seeds,
        [float(series[left][seed]) for seed in seeds],
        [float(series[right][seed]) for seed in seeds],
    )


def compare_pairs(
    series: Mapping[str, Mapping[int, float]],
    comparisons: Sequence[tuple[str, str, str]],
    *,
    alpha: float = 0.05,
) -> tuple[ComparisonResult, ...]:
    """按同一组 seed 做配对 Wilcoxon，再统一 Holm 校正。"""

    raw: list[tuple[str, str, str, tuple[int, ...], PairedTest]] = []
    for name, left, right in comparisons:
        seeds, left_values, right_values = _aligned(series, left, right)
        raw.append(
            (
                name,
                left,
                right,
                seeds,
                paired_wilcoxon(left_values, right_values),
            )
        )
    corrected = holm_bonferroni(
        {name: test.p_value for name, _, _, _, test in raw},
        alpha=alpha,
    )
    by_name = {item.name: item for item in corrected}
    return tuple(
        ComparisonResult(
            name=name,
            left=left,
            right=right,
            test=test,
            holm=by_name[name],
            seeds=seeds,
        )
        for name, left, right, seeds, test in raw
    )


def to_json(
    series: Mapping[str, Mapping[int, float]],
    comparisons: Sequence[ComparisonResult] | None = None,
) -> dict[str, Any]:
    """机器可读报告：原始逐 seed 值 + 分布 + 配对比较。"""

    return {
        "series": {
            strategy: {str(seed): value for seed, value in sorted(values.items())}
            for strategy, values in sorted(series.items())
        },
        "distributions": {
            strategy: asdict(summarize_distribution(list(values.values())))
            for strategy, values in sorted(series.items())
        },
        "comparisons": [
            {
                "name": result.name,
                "left": result.left,
                "right": result.right,
                "seeds": list(result.seeds),
                "mean_difference": result.test.mean_difference,
                "median_difference": result.test.median_difference,
                "statistic": result.test.statistic,
                "p_value": result.test.p_value,
                "adjusted_p": result.holm.adjusted_p,
                "rejected": result.holm.rejected,
                "method": result.test.method,
                "rank_biserial": result.test.rank_biserial,
            }
            for result in (comparisons or ())
        ],
    }


__all__ = [
    "ComparisonResult",
    "Distribution",
    "ReportError",
    "compare_pairs",
    "summarize_distribution",
    "to_json",
]
