"""M1/M2 只差跨 run 历史，并在矩阵里正确累积记忆。"""

from __future__ import annotations

from pathlib import Path

from src.experiment import ExperimentSpec, OfflineExecutor, run_matrix
from src.experiment.session import load_stage1

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "stage1_config.json"
M1 = "src.baselines.m1_memory_cold:run"
M2 = "src.baselines.m2_memory_warm:run"


def _spec() -> ExperimentSpec:
    truth, prefixes, _ = load_stage1(CONFIG)
    aliased = [p for p, fields in truth.prefixes.items() if fields["aliased"]]

    def factory(_seed: int):
        return OfflineExecutor(aliased, prefixes)

    return ExperimentSpec(
        config_path=CONFIG,
        executor_factory=factory,
        reset_target=lambda seed: None,
        cwd=REPO_ROOT,
    )


class TestM1M2Matrix:
    def test_the_first_run_is_identical_and_only_m2_accumulates(self) -> None:
        result = run_matrix(_spec(), [M1, M2], [0, 1])
        grouped = result.by_strategy()
        m1 = grouped[M1]
        m2 = grouped[M2]

        assert m1[0].score == m2[0].score
        assert m2[1].score != m1[1].score

        assert m1[0].session.memory and m1[1].session.memory
        assert sum(e["pulls"] for e in m1[0].session.memory.values()) == 0
        assert sum(e["pulls"] for e in m1[1].session.memory.values()) == 0
        assert sum(e["pulls"] for e in m2[0].session.memory.values()) == 300
        assert sum(e["pulls"] for e in m2[1].session.memory.values()) == 600

    def test_m2_still_spends_exactly_one_budget_per_run(self) -> None:
        rows = run_matrix(_spec(), [M2], [0, 1]).rows
        assert all(row.session.charged == 300 for row in rows)
        assert all(row.session.probes_run == 300 for row in rows)
