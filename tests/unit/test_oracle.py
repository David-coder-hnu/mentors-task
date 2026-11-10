"""Oracle 成功路径：真值出口与靶场重置都只在 runner 侧。"""

from __future__ import annotations

import pytest

from src.adapter.oracle import Oracle
from src.adapter.truth import Stage1Truth

TRUTH = Stage1Truth(prefixes={"2001:db8:1::/64": {"aliased": True}})


class TestOracle:
    def test_ground_truth_returns_the_current_source_value(self) -> None:
        oracle = Oracle(truth_source=lambda: TRUTH, reset_target=lambda seed: None)
        assert oracle.ground_truth() is TRUTH

    def test_reset_target_forwards_the_seed_and_logs_it(self) -> None:
        seen: list[int] = []
        oracle = Oracle(truth_source=lambda: TRUTH, reset_target=seen.append)
        oracle.reset_target(7)
        oracle.reset_target(8)
        assert seen == [7, 8]
        assert oracle.reset_log == [7, 8]

    @pytest.mark.parametrize("seed", [True, 1.5, "7"])
    def test_reset_target_rejects_non_integer_seed(self, seed) -> None:
        oracle = Oracle(truth_source=lambda: TRUTH, reset_target=lambda value: None)
        with pytest.raises(TypeError, match="seed 必须是整数"):
            oracle.reset_target(seed)
