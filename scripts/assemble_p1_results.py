"""Assemble a reproducible P1 evidence manifest from runner outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "p1" / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_record(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{path} 没有 rows 数组")
    strategies = sorted(
        {
            str(row["strategy"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("strategy"), str)
        }
    )
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "agent_provenance": document.get("agent_provenance"),
        "completion_command": document.get("completion_command"),
        "decision_cache": document.get("decision_cache"),
        "rows": len(rows),
        "strategies": strategies,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="预注册 P1 manifest",
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument(
        "--provenance",
        required=True,
        choices=("completion-command", "cache-replay", "fixture"),
    )
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    scenarios = {
        str(item["id"]): item
        for item in manifest.get("scenarios", [])
        if isinstance(item, dict) and "id" in item
    }
    if args.scenario not in scenarios:
        raise SystemExit(f"未知预注册场景：{args.scenario}")
    scenario = scenarios[args.scenario]

    sources = []
    seen_strategies: set[str] = set()
    for path in args.source:
        record = _source_record(path)
        overlap = seen_strategies.intersection(record["strategies"])
        if overlap:
            raise SystemExit(f"多个来源重复包含策略：{sorted(overlap)}")
        seen_strategies.update(record["strategies"])
        sources.append(record)

    evidence = {
        "schema": "p1-evidence-manifest-v1",
        "provenance": args.provenance,
        "scenario": {
            "id": args.scenario,
            "config": scenario["config"],
            "budget": scenario["budget"],
            "epoch_size": scenario["epoch_size"],
        },
        "sources": sources,
        "results": [
            {"id": args.scenario, "output": source["path"]}
            for source in sources
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
