"""T11：配对 Wilcoxon、Holm 校正与逐 seed 报告。"""

from __future__ import annotations

import math
import statistics

import pytest

from src.scoring.report import (
    ReportError,
    compare_pairs,
    summarize_distribution,
    to_json,
)
from src.scoring.stats import (
    StatsError,
    holm_bonferroni,
    paired_wilcoxon,
)


class TestWilcoxon:
    def test_all_positive_differences_are_significant(self) -> None:
        left = [1.0, 2.0, 3.0, 4.0, 5.0]
        right = [0.0] * 5
        result = paired_wilcoxon(left, right)
        assert result.method == "exact"
        assert result.p_value == pytest.approx(2 / 32)
        assert result.rank_biserial == 1.0

    def test_identical_samples_give_p_one(self) -> None:
        result = paired_wilcoxon([1, 2, 3], [1, 2, 3])
        assert result.method == "degenerate"
        assert result.p_value == 1.0
        assert result.n_nonzero == 0

    def test_mixed_differences_are_two_sided(self) -> None:
        result = paired_wilcoxon([4, 3, 2, 1], [1, 2, 3, 4])
        assert result.mean_difference == 0.0
        assert result.p_value == 1.0

    def test_ties_use_average_ranks(self) -> None:
        result = paired_wilcoxon([2, 2, 2], [0, 0, 0])
        assert result.statistic == pytest.approx(6.0)
        assert result.p_value == pytest.approx(2 / 8)

    def test_length_mismatch_is_refused(self) -> None:
        with pytest.raises(StatsError, match="长度不同"):
            paired_wilcoxon([1, 2], [1])

    def test_empty_is_refused(self) -> None:
        with pytest.raises(StatsError, match="不能为空"):
            paired_wilcoxon([], [])

    def test_a_paired_design_beats_non_overlapping_ci_as_a_gate(self) -> None:
        """两组均值的普通 95% CI 重叠，但逐 seed 差值高度一致。"""

        noise = [0.08 * math.sin(i * 1.7) for i in range(30)]
        left = [0.5 + value for value in noise]
        right = [value - 0.015 for value in left]

        def ci(values):
            half = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
            mean = statistics.fmean(values)
            return mean - half, mean + half

        left_low, left_high = ci(left)
        right_low, right_high = ci(right)
        assert max(left_low, right_low) <= min(left_high, right_high)

        result = paired_wilcoxon(left, right)
        assert result.p_value < 0.05
        assert result.mean_difference == pytest.approx(0.015)


class TestHolm:
    def test_step_down_adjustment(self) -> None:
        result = holm_bonferroni({"a": 0.01, "b": 0.04, "c": 0.03})
        assert [item.name for item in result] == ["a", "c", "b"]
        assert result[0].adjusted_p == pytest.approx(0.03)
        assert result[1].adjusted_p == pytest.approx(0.06)
        assert result[2].adjusted_p == pytest.approx(0.06)
        assert [item.rejected for item in result] == [True, False, False]

    def test_once_a_hypothesis_fails_all_later_ones_fail(self) -> None:
        result = holm_bonferroni({"a": 0.04, "b": 0.01})
        # b 校正后 0.02 通过；a 校正后 0.04、但 step-down 在更小 p 之后仍可继续。
        assert result[0].name == "b" and result[0].rejected
        assert result[1].name == "a" and result[1].rejected

    @pytest.mark.parametrize("p", [-0.1, 1.1, float("nan")])
    def test_invalid_p_values_are_refused(self, p: float) -> None:
        with pytest.raises(StatsError):
            holm_bonferroni({"x": p})


class TestReport:
    def test_distribution_keeps_seed_shape(self) -> None:
        summary = summarize_distribution([0.1, 0.2, 0.3, 1.0])
        assert summary.n == 4
        assert summary.minimum == 0.1
        assert summary.maximum == 1.0
        assert summary.median == pytest.approx(0.25)
        assert summary.ci_low <= summary.mean <= summary.ci_high

    def test_comparisons_require_exact_same_seeds(self) -> None:
        series = {
            "a": {0: 1.0, 1: 2.0},
            "b": {0: 0.5},
        }
        with pytest.raises(ReportError, match="seed 集合不同"):
            compare_pairs(series, [("a-b", "a", "b")])

    def test_holm_is_applied_across_comparisons(self) -> None:
        series = {
            "a": {seed: float(seed) + 2 for seed in range(10)},
            "b": {seed: float(seed) for seed in range(10)},
            "c": {seed: float(seed) + 1 for seed in range(10)},
        }
        results = compare_pairs(
            series,
            [("a-b", "a", "b"), ("c-b", "c", "b")],
        )
        assert len(results) == 2
        assert all(result.holm.adjusted_p >= result.test.p_value for result in results)

    def test_json_contains_raw_seed_values_and_distributions(self) -> None:
        series = {"a": {0: 1.0, 1: 2.0}, "b": {0: 0.5, 1: 0.9}}
        comparisons = compare_pairs(series, [("a-b", "a", "b")])
        document = to_json(series, comparisons)
        assert document["series"]["a"] == {"0": 1.0, "1": 2.0}
        assert document["distributions"]["a"]["n"] == 2
        assert document["comparisons"][0]["adjusted_p"] == pytest.approx(
            comparisons[0].holm.adjusted_p
        )
