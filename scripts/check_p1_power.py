"""Print preregistered P1 power sensitivity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.scoring.power import paired_normal_power  # noqa: E402


def main() -> int:
    config = json.loads((ROOT / "config" / "p1" / "power.json").read_text(encoding="utf-8"))
    for endpoint, values in config["endpoints"].items():
        power = paired_normal_power(
            effect=float(values["mde"]),
            paired_sd=float(values["paired_sd_ceiling"]),
            n=int(config["n"]),
            alpha=float(config["alpha"]),
            simulations=int(config["simulations"]),
            seed=int(config["seed"]),
        )
        status = "PASS" if power >= float(config["target_power"]) else "FAIL"
        print(
            f"{endpoint}: N={config['n']} MDE={values['mde']} "
            f"SD={values['paired_sd_ceiling']} power={power:.4f} {status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
