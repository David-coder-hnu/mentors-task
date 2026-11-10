"""A1/A2/A3 analysis keeps fixture provenance distinct from model evidence."""

from __future__ import annotations

from scripts.analyze_ablation import A1, A2, A3, analyze, markdown


def _row(strategy: str, seed: int, routers: int, waste: float, caught: int):
    return {
        "strategy": strategy,
        "seed": seed,
        "stage2": {"true_routers": routers},
        "aliased_waste_ratio": waste,
        "aliased_caught": caught,
    }


def test_fixture_provenance_is_explicit_in_report() -> None:
    rows = []
    for seed in range(3):
        rows.extend(
            [
                _row(A1, seed, 3, 0.0, 0),
                _row(A2, seed, 29 + seed, 0.248, 8),
                _row(A3, seed, 29 + seed, 0.248, 8),
            ]
        )
    report = analyze(
        {
            "rows": rows,
            "agent_provenance": "completion-command",
            "completion_command": "python tests/fixtures/deterministic_agent_completion.py",
        }
    )
    assert report["provenance"] == "fixture"
    text = markdown(report)
    assert "这不是模型结果" in text
    assert "A3_vs_A2" in text
