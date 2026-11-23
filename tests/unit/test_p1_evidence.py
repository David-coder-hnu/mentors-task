"""P1 evidence assembly, grouped analysis and live-output audit."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_p1 import (
    A1,
    A2,
    A3,
    B5,
    B5O,
    main as analyze_main,
)
from scripts.make_p1_audit import main as audit_main


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _row(strategy: str, seed: int, routers: int, waste: float) -> dict[str, object]:
    return {
        "strategy": strategy,
        "seed": seed,
        "stage2": {"true_routers": routers},
        "aliased_waste_ratio": waste,
        "aliased_caught": 8,
    }


def test_analysis_groups_multiple_sources_for_one_scenario(tmp_path: Path) -> None:
    agent_rows = []
    baseline_rows = []
    for seed in range(30):
        agent_rows.extend(
            [
                _row(A1, seed, 5, 0.8),
                _row(A2, seed, 20, 0.3),
                _row(A3, seed, 19, 0.3),
            ]
        )
        baseline_rows.extend(
            [
                _row(B5, seed, 30, 0.2),
                _row(B5O, seed, 29, 0.2),
            ]
        )
    agent = tmp_path / "agent.json"
    baseline = tmp_path / "baseline.json"
    _write(agent, {"rows": agent_rows, "agent_provenance": "completion-command"})
    _write(baseline, {"rows": baseline_rows, "agent_provenance": "not-applicable"})
    manifest = tmp_path / "manifest.json"
    _write(
        manifest,
        {
            "provenance": "completion-command",
            "results": [
                {"id": "broad", "output": str(agent)},
                {"id": "broad", "output": str(baseline)},
            ],
        },
    )

    analyze_main([str(manifest)])

    report = json.loads((tmp_path / "p1-analysis.json").read_text(encoding="utf-8"))
    assert set(report["scenarios"]) == {"broad"}
    broad = report["scenarios"]["broad"]
    assert broad["provenance"] == "completion-command"
    assert broad["a2_success"] is True
    assert broad["b5_open_success"] is False
    assert broad["cell"] == "p1-supported"


def test_live_audit_accepts_direct_output_and_counts_step_only_compose(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.json"
    _write(
        source,
        {
            "config": "config/stage2_config.json",
            "agent_provenance": "completion-command",
            "rows": [
                {
                    "strategy": A3,
                    "seed": 0,
                    "decision_records": [
                        {
                            "index": 0,
                            "action": None,
                            "cache_hit": False,
                            "elapsed_s": 1.0,
                            "raw": '{"target":"balanced","fraction":0.2}',
                        }
                    ],
                }
            ],
        },
    )
    output = tmp_path / "audit"

    audit_main([str(source), "--out", str(output)])

    summary = json.loads((output / "audit-summary.json").read_text(encoding="utf-8"))
    assert summary["provenance"] == "completion-command"
    assert summary["completion_calls"] == 1
    assert summary["model_calls"] == 1
    assert summary["construction_attempts"] == 1
    assert summary["actions"] == {"compose": 1}
    attempt = json.loads(
        (output / "construction_attempts.jsonl").read_text(encoding="utf-8").strip()
    )
    assert attempt["scenario"] == "broad"
    assert attempt["effective_action"] == "compose"
    assert attempt["compatibility"] == "step-only-compatibility"
