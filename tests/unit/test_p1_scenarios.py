"""P1 scenarios are pre-registered, loadable and non-degenerate."""

from __future__ import annotations

import json
from pathlib import Path

from src.baselines.b5_open import B5_OPEN_PROGRAM, FROZEN_CANDIDATE
from src.experiment.stage2 import load_stage2_context

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "config" / "p1" / "manifest.json"
FROZEN = ROOT / "config" / "p1" / "b5_open_frozen.json"


def test_all_preregistered_p1_scenarios_load() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert len(manifest["scenarios"]) >= 3
    ids = set()
    for scenario in manifest["scenarios"]:
        ids.add(scenario["id"])
        context = load_stage2_context(ROOT / scenario["config"])
        assert len(context.prefixes) >= 10
        assert any(fields["aliased"] for fields in context.truth.prefixes.values())
        assert any(not fields["aliased"] for fields in context.truth.prefixes.values())
    assert {"broad", "prefix_diversity", "sparse_trap"} <= ids


def test_primary_comparisons_are_pre_registered() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    names = {item[0] for item in manifest["comparisons"]}
    assert names == {
        "A2_vs_A1",
        "A2_vs_B5_open",
        "B5_open_vs_B5_closed",
    }


def test_b5_open_code_matches_the_heldout_freeze_record() -> None:
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    assert frozen["candidate"] == FROZEN_CANDIDATE
    assert frozen["program"] == [
        {"target": step.target, "fraction": step.fraction}
        for step in B5_OPEN_PROGRAM
    ]
    assert frozen["tuning_config"].endswith("b5_open_tuning.json")
