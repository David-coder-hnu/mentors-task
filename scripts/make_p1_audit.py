"""Turn P1 scenario outputs into the equal-effort audit package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _records(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError("scenario 输出没有 rows")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest_results", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = json.loads(args.manifest_results.read_text(encoding="utf-8"))
    output = args.out or args.manifest_results.parent / "audit"
    output.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema": "config/protocols/primitive_schema.json",
        "program_schema": "config/protocols/program_schema.json",
        "max_new_primitives": 1,
        "initial_primitives": {
            "A1": ["sample"],
            "A2": ["sample", "construct", "compose"],
            "A3": ["sample", "construct", "compose"],
            "B5_closed": ["probe", "sample", "report"],
            "B5_open": ["probe", "sample", "report", "compose"],
        },
        "provenance": summary.get("provenance"),
    }
    (output / "tool_pool_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    attempts = []
    usage = []
    for item in summary["results"]:
        scenario = str(item["id"])
        document = json.loads(Path(item["output"]).read_text(encoding="utf-8"))
        for row in _records(document):
            strategy = str(row["strategy"])
            seed = int(row["seed"])
            observations = output / "epoch_observations" / strategy.replace(":", "_") / scenario
            observations.mkdir(parents=True, exist_ok=True)
            with (observations / f"{seed}.jsonl").open("w", encoding="utf-8") as handle:
                for decision in row.get("decision_records", []):
                    handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
                    usage.append(
                        {
                            "scenario": scenario,
                            "strategy": strategy,
                            "seed": seed,
                            "index": decision["index"],
                            "cache_hit": decision["cache_hit"],
                            "elapsed_s": decision["elapsed_s"],
                            "model_call": not decision["cache_hit"],
                        }
                    )
                    if decision.get("action") in {"construct", "compose"}:
                        attempts.append(
                            {
                                "scenario": scenario,
                                "strategy": strategy,
                                "seed": seed,
                                **decision,
                            }
                        )

    for name, rows in (
        ("construction_attempts.jsonl", attempts),
        ("api_usage.jsonl", usage),
    ):
        with (output / name).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    (output / "adjudication.json").write_text(
        json.dumps(
            {
                "status": "pending-human-adjudication",
                "rule": "docs/protocols/equal_effort.md",
                "construction_attempts": len(attempts),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
