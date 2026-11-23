"""Tune and freeze B5-open programs on the independent held-out topology."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.baselines.b5_open import B5_OPEN_PROGRAMS  # noqa: E402
from src.baselines.b5_open_candidates import ENTRY_POINTS  # noqa: E402
from src.experiment.driver import _parse_seeds  # noqa: E402

CONFIG = ROOT / "config" / "p1" / "b5_open_tuning.json"
OUT = ROOT / "docker" / "run" / "p1-b5-open-tuning.json"
FROZEN = ROOT / "config" / "p1" / "b5_open_frozen.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default=None)
    args = parser.parse_args(argv)
    document = json.loads(CONFIG.read_text(encoding="utf-8"))
    seeds = args.seeds or str(document["tuning_seeds"])
    command = [
        sys.executable,
        "-m",
        "src.experiment",
        "--stage",
        "2",
        "--config",
        str(CONFIG),
        "--seeds",
        seeds,
        "--out",
        str(OUT),
    ]
    for entry in ENTRY_POINTS.values():
        command.extend(["--strategy", entry])
    subprocess.run(command, cwd=ROOT, check=True)

    result = json.loads(OUT.read_text(encoding="utf-8"))
    by_candidate = {}
    reverse = {value: key for key, value in ENTRY_POINTS.items()}
    for row in result["rows"]:
        name = reverse[str(row["strategy"])]
        stage2 = row["stage2"]
        bucket = by_candidate.setdefault(
            name, {"true_routers": [], "aliased_waste_ratio": [], "aliased_caught": []}
        )
        bucket["true_routers"].append(stage2["true_routers"])
        bucket["aliased_waste_ratio"].append(row["aliased_waste_ratio"])
        bucket["aliased_caught"].append(row["aliased_caught"])

    stats = {
        name: {
            "mean_true_routers": sum(values["true_routers"]) / len(values["true_routers"]),
            "mean_aliased_waste_ratio": sum(values["aliased_waste_ratio"])
            / len(values["aliased_waste_ratio"]),
            "mean_aliased_caught": sum(values["aliased_caught"])
            / len(values["aliased_caught"]),
        }
        for name, values in by_candidate.items()
    }
    selected = min(
        stats,
        key=lambda name: (
            -stats[name]["mean_true_routers"],
            stats[name]["mean_aliased_waste_ratio"],
            name,
        ),
    )
    frozen = {
        "candidate": selected,
        "program": [
            {"target": step.target, "fraction": step.fraction}
            for step in B5_OPEN_PROGRAMS[selected]
        ],
        "tuning_config": str(CONFIG.relative_to(ROOT)),
        "tuning_config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "tuning_seeds": list(_parse_seeds(seeds)),
        "candidates": stats,
        "selection_rule": "max mean true_routers; tie -> min aliased waste; tie -> name",
    }
    FROZEN.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"selected": selected, "stats": stats[selected]}, ensure_ascii=False))
    print(FROZEN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
