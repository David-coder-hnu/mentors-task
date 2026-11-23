"""P1 2x2-style analysis from preregistered scenario matrices."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.report import compare_pairs, to_json  # noqa: E402

A1 = "src.agent.strategies:run_a1"
A2 = "src.agent.strategies:run_a2"
A3 = "src.agent.strategies:run_a3"
B5 = "src.baselines.b5_frp_aware:run"
B5O = "src.baselines.b5_open:run"
COMPARISONS = (
    ("A2_vs_A1", A2, A1),
    ("A2_vs_B5_open", A2, B5O),
    ("B5_open_vs_B5_closed", B5O, B5),
)


def _series(rows: list[Mapping[str, object]], endpoint: str) -> dict[str, dict[int, float]]:
    out = {name: {} for name in (A1, A2, A3, B5, B5O)}
    for row in rows:
        strategy = row.get("strategy")
        seed = row.get("seed")
        if strategy not in out or not isinstance(seed, int):
            continue
        if endpoint == "true_routers":
            stage2 = row.get("stage2")
            value = stage2.get("true_routers") if isinstance(stage2, Mapping) else None
        else:
            value = row.get(endpoint)
        if isinstance(value, (int, float)):
            out[strategy][seed] = float(value)
    return out


def analyze_scenario(document: Mapping[str, object]) -> dict[str, object]:
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError("scenario 输入没有 rows")
    endpoints = {}
    for endpoint in ("true_routers", "aliased_waste_ratio", "aliased_caught"):
        series = _series(rows, endpoint)
        endpoints[endpoint] = to_json(series, compare_pairs(series, COMPARISONS))

    router_comparisons = {
        item["name"]: item for item in endpoints["true_routers"]["comparisons"]
    }
    a2_success = bool(router_comparisons["A2_vs_A1"]["rejected"]) and (
        router_comparisons["A2_vs_A1"]["mean_difference"] > 0
    )
    b5_success = bool(router_comparisons["B5_open_vs_B5_closed"]["rejected"]) and (
        router_comparisons["B5_open_vs_B5_closed"]["mean_difference"] > 0
    )
    if a2_success and b5_success:
        cell = "open-space-supported_llm-no-additive-value"
    elif a2_success and not b5_success:
        cell = "p1-supported"
    elif not a2_success and b5_success:
        cell = "p1-killed"
    else:
        cell = "scenario-inconclusive"
    if document.get("agent_provenance") != "completion-command":
        cell = f"fixture-only:{cell}"
    return {
        "provenance": document.get("agent_provenance"),
        "cell": cell,
        "a2_success": a2_success,
        "b5_open_success": b5_success,
        "endpoints": endpoints,
    }


def markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# P1 open-action-space analysis",
        "",
        f"Provenance: `{report['provenance']}`",
        "",
    ]
    if report["provenance"] != "completion-command":
        lines.extend(
            [
                "> **这不是模型结果。** 该文件只验证开放动作空间、B5-open 和统计链路。",
                "",
            ]
        )
    for scenario_id, result in report["scenarios"].items():
        lines.extend(
            [
                f"## {scenario_id}",
                "",
                f"- 2x2 cell: `{result['cell']}`",
                f"- A2 beats A1: `{result['a2_success']}`",
                f"- B5-open beats B5-closed: `{result['b5_open_success']}`",
                "",
            ]
        )
        for endpoint, payload in result["endpoints"].items():
            lines.append(f"### {endpoint}")
            lines.append("")
            for item in payload["comparisons"]:
                lines.append(
                    f"- `{item['name']}`: diff={item['mean_difference']:.6f}, "
                    f"Holm p={item['adjusted_p']:.6g}, rejected={item['rejected']}"
                )
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest_results", type=Path)
    args = parser.parse_args(argv)
    source = json.loads(args.manifest_results.read_text(encoding="utf-8"))
    scenarios = {}
    provenance = source.get("provenance")
    for item in source["results"]:
        document = json.loads(Path(item["output"]).read_text(encoding="utf-8"))
        provenance = provenance or document.get("agent_provenance")
        result = analyze_scenario(document)
        result["provenance"] = provenance
        scenarios[str(item["id"])] = result
    report = {"provenance": provenance, "scenarios": scenarios}
    out = args.manifest_results.with_name("p1-analysis.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report) + "\n", encoding="utf-8")
    print(out)
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
