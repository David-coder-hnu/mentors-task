"""Turn P1 scenario outputs into the equal-effort audit package.

The input may be a fixture manifest or one direct runner output. A manifest can
contain multiple sources for the same scenario, which keeps real-model agent
rows and deterministic B5 rows in one auditable package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _records(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError("scenario 输出没有 rows")
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _infer_scenario(path: Path, document: dict[str, Any]) -> str:
    config = document.get("config")
    if isinstance(config, str):
        name = Path(config).stem
        if name == "stage2_config":
            return "broad"
        if name in {"prefix_diversity", "sparse_trap", "hot_hierarchy"}:
            return name
    return path.stem


def _input_documents(
    source: Path,
    *,
    scenario_override: str | None,
) -> tuple[dict[str, Any], list[tuple[str, Path, dict[str, Any]]]]:
    summary = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(summary.get("rows"), list):
        scenario = scenario_override or _infer_scenario(source, summary)
        return (
            {
                "provenance": summary.get("agent_provenance"),
                "sources": [
                    {
                        "path": str(source),
                        "sha256": _sha256(source),
                        "agent_provenance": summary.get("agent_provenance"),
                    }
                ],
            },
            [(scenario, source, summary)],
        )

    results = summary.get("results")
    if not isinstance(results, list):
        raise ValueError("输入既不是 runner 输出，也不是含 results 的 manifest")
    documents: list[tuple[str, Path, dict[str, Any]]] = []
    sources = []
    for item in results:
        if not isinstance(item, dict) or "output" not in item:
            raise ValueError("manifest result 缺少 output")
        path = Path(item["output"])
        document = json.loads(path.read_text(encoding="utf-8"))
        scenario = scenario_override or str(item.get("id") or _infer_scenario(path, document))
        documents.append((scenario, path, document))
        sources.append(
            {
                "path": str(path),
                "sha256": _sha256(path),
                "agent_provenance": document.get("agent_provenance"),
            }
        )
    return (
        {"provenance": summary.get("provenance"), "sources": sources},
        documents,
    )


def _effective_action(decision: dict[str, Any]) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(str(decision.get("raw", "")))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    action = payload.get("action")
    if isinstance(action, str):
        return action, None
    if set(payload) == {"target", "fraction"}:
        return "compose", "step-only-compatibility"
    return None, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest_results", type=Path)
    parser.add_argument("--scenario", default=None, help="直接 runner 输出的场景 ID")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    summary, documents = _input_documents(
        args.manifest_results,
        scenario_override=args.scenario,
    )
    output = args.out or args.manifest_results.parent / "audit"
    output.mkdir(parents=True, exist_ok=True)

    freeze_path = (
        Path(__file__).resolve().parents[1] / "config" / "p1" / "b5_open_frozen.json"
    )
    manifest = {
        "schema": "config/protocols/primitive_schema.json",
        "program_schema": "config/protocols/program_schema.json",
        "max_new_primitives": 1,
        "b5_open_freeze": "config/p1/b5_open_frozen.json",
        "initial_primitives": {
            "A1": ["sample"],
            "A2": ["sample", "construct", "compose"],
            "A3": ["sample", "construct", "compose"],
            "B5_closed": ["probe", "sample", "report"],
            "B5_open": ["probe", "sample", "report", "compose"],
        },
        "provenance": summary.get("provenance"),
        "a2_runtime_construction": True,
        "b5_pre_run_construction": True,
        "b5_open_freeze_sha256": _sha256(freeze_path),
        "sources": summary["sources"],
    }
    (output / "tool_pool_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    attempts = []
    usage = []
    actions: dict[str, int] = {}
    model_provenance = summary.get("provenance") == "completion-command"
    for scenario, _path, document in documents:
        for row in _records(document):
            strategy = str(row["strategy"])
            seed = int(row["seed"])
            observations = output / "epoch_observations" / strategy.replace(":", "_") / scenario
            observations.mkdir(parents=True, exist_ok=True)
            with (observations / f"{seed}.jsonl").open("w", encoding="utf-8") as handle:
                for decision in row.get("decision_records", []):
                    effective_action, compatibility = _effective_action(decision)
                    actions[effective_action or "unparsed"] = (
                        actions.get(effective_action or "unparsed", 0) + 1
                    )
                    handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
                    usage.append(
                        {
                            "scenario": scenario,
                            "strategy": strategy,
                            "seed": seed,
                            "index": decision["index"],
                            "cache_hit": decision["cache_hit"],
                            "elapsed_s": decision["elapsed_s"],
                            "completion_call": not decision["cache_hit"],
                            "model_call": model_provenance and not decision["cache_hit"],
                        }
                    )
                    if effective_action in {"construct", "compose"}:
                        attempts.append(
                            {
                                "scenario": scenario,
                                "strategy": strategy,
                                "seed": seed,
                                "effective_action": effective_action,
                                "compatibility": compatibility,
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
                "decision_records": len(usage),
                "completion_calls": sum(
                    1 for item in usage if item["completion_call"]
                ),
                "model_calls": sum(1 for item in usage if item["model_call"]),
                "cache_hits": sum(1 for item in usage if item["cache_hit"]),
                "actions": dict(sorted(actions.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "audit-summary.json").write_text(
        json.dumps(
            {
                "provenance": summary.get("provenance"),
                "sources": summary["sources"],
                "decision_records": len(usage),
                "completion_calls": sum(
                    1 for item in usage if item["completion_call"]
                ),
                "model_calls": sum(1 for item in usage if item["model_call"]),
                "cache_hits": sum(1 for item in usage if item["cache_hit"]),
                "construction_attempts": len(attempts),
                "actions": dict(sorted(actions.items())),
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
