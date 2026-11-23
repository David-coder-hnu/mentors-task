"""P1 analysis labels fixture cells without turning them into model evidence."""

from __future__ import annotations

from scripts.analyze_p1 import A1, A2, A3, B5, B5O, analyze_scenario, markdown


def _row(strategy: str, seed: int, routers: int, waste: float = 0.2):
    return {
        "strategy": strategy,
        "seed": seed,
        "stage2": {"true_routers": routers},
        "aliased_waste_ratio": waste,
        "aliased_caught": 8,
    }


def test_fixture_p1_cell_is_never_reported_as_model_evidence() -> None:
    rows = []
    for seed in range(10):
        rows.extend(
            [
                _row(A1, seed, 3),
                _row(A2, seed, 30),
                _row(A3, seed, 30),
                _row(B5, seed, 40, 0.2),
                _row(B5O, seed, 20, 0.4),
            ]
        )
    report = analyze_scenario(
        {"rows": rows, "agent_provenance": "fixture"}
    )
    assert report["a2_success"] is True
    assert report["b5_open_success"] is False
    assert str(report["cell"]).startswith("fixture-only:")
    assert "这不是模型结果" in markdown(
        {"provenance": "fixture", "scenarios": {"x": report}}
    )
