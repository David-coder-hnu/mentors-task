"""Stage 1 分析脚本：端点提取与共主判决。"""

from __future__ import annotations

from scripts.analyze_smoke_table import (
    B0,
    B1,
    B4,
    COMPARISONS,
    M1,
    M2,
    co_primary_decision,
)


def _result(name, mean, rejected=True):
    class Test:
        mean_difference = mean

    class Holm:
        pass

    holm = Holm()
    holm.rejected = rejected

    class Result:
        pass

    result = Result()
    result.name = name
    result.test = Test()
    result.holm = holm
    return result


class TestDecision:
    def test_left_wins_when_one_endpoint_better_and_other_not_worse(self) -> None:
        analyses = {
            "aliased_waste_ratio": [
                _result("M2_vs_B4", -0.1, True),
                _result("M2_vs_M1", 0.0, False),
            ],
            "aliased_caught": [
                _result("M2_vs_B4", 0.0, False),
                _result("M2_vs_M1", 0.0, False),
            ],
        }
        decision = co_primary_decision(
            "M2_vs_B4",
            analyses,
            admissible={B0: True, B1: True, B4: True, M1: True, M2: True},
        )
        assert decision["decision"] == "left"

    def test_conflicting_endpoints_are_inconclusive(self) -> None:
        analyses = {
            "aliased_waste_ratio": [
                _result("M2_vs_B4", -0.1, True),
                _result("M2_vs_M1", 0.0, False),
            ],
            "aliased_caught": [
                _result("M2_vs_B4", -1.0, True),
                _result("M2_vs_M1", 0.0, False),
            ],
        }
        decision = co_primary_decision(
            "M2_vs_B4",
            analyses,
            admissible={B0: True, B1: True, B4: True, M1: True, M2: True},
        )
        assert decision["decision"] == "inconclusive"

    def test_inadmissible_alias_endpoint_invalidates_comparison(self) -> None:
        analyses = {
            "aliased_waste_ratio": [_result("M2_vs_B4", 0, False)],
            "aliased_caught": [_result("M2_vs_B4", 0, False)],
        }
        decision = co_primary_decision(
            "M2_vs_B4",
            analyses,
            admissible={B0: True, B1: True, B4: False, M1: True, M2: True},
        )
        assert decision["decision"] == "invalid"


def test_comparison_names_are_the_registered_pair() -> None:
    assert COMPARISONS == (
        ("M2_vs_B4", M2, B4),
        ("M2_vs_M1", M2, M1),
    )
