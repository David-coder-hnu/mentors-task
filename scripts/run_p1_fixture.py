"""Run all preregistered P1 scenarios with the deterministic engineering fixture."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "config" / "p1" / "manifest.json"
OUTPUT_DIR = REPO_ROOT / "docker" / "run" / "p1-fixture"
ROWS = (
    "src.agent.strategies:run_a1",
    "src.agent.strategies:run_a2",
    "src.agent.strategies:run_a3",
    "src.baselines.b5_frp_aware:run",
    "src.baselines.b5_open:run",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default=None, help="覆盖 manifest 中的 seeds")
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    seeds = args.seeds or str(manifest["seeds"])
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in manifest["scenarios"]:
        scenario_id = str(scenario["id"])
        output = OUTPUT_DIR / f"{scenario_id}.json"
        cache = OUTPUT_DIR / f"{scenario_id}.cache.jsonl"
        command = [
            sys.executable,
            "-m",
            "src.experiment",
            "--stage",
            "2",
            "--config",
            str(REPO_ROOT / str(scenario["config"])),
            "--seeds",
            seeds,
            "--completion-command",
            f"{sys.executable} {REPO_ROOT / 'tests' / 'fixtures' / 'deterministic_agent_completion.py'}",
            "--decision-cache",
            str(cache),
            "--out",
            str(output),
        ]
        for row in ROWS:
            command.extend(["--strategy", row])
        print(f"[p1] scenario={scenario_id} seeds={seeds}", flush=True)
        subprocess.run(command, cwd=REPO_ROOT, check=True)
        results.append({"id": scenario_id, "output": str(output)})
    summary = {"provenance": "fixture", "seeds": seeds, "results": results}
    target = OUTPUT_DIR / "manifest-results.json"
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
