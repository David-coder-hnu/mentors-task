"""A1/A2/A3 同 seed 配对分析。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.report import compare_pairs, to_json  # noqa: E402

A1 = "src.agent.strategies:run_a1"
A2 = "src.agent.strategies:run_a2"
A3 = "src.agent.strategies:run_a3"
STRATEGIES = (A1, A2, A3)
COMPARISONS = (
    ("A2_vs_A1", A2, A1),
    ("A3_vs_A2", A3, A2),
)
ENDPOINTS = ("true_routers", "aliased_waste_ratio", "aliased_caught")


def _rows(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    rows = document.get("rows")
    if isinstance(rows, Mapping):
        rows = rows.get("rows")
    if not isinstance(rows, list):
        raise ValueError("输入没有 rows 数组")
    return rows


def series_for(rows: Sequence[Mapping[str, object]], endpoint: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {strategy: {} for strategy in STRATEGIES}
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
    for strategy in STRATEGIES:
        if not out[strategy]:
            raise ValueError(f"{strategy} 没有 {endpoint}")
    return out


def analyze(document: Mapping[str, object]) -> dict[str, object]:
    rows = _rows(document)
    command = document.get("completion_command")
    provenance = (
        "fixture"
        if isinstance(command, str) and "deterministic_agent_completion" in command
        else document.get("agent_provenance")
    )
    endpoints = {}
    for endpoint in ENDPOINTS:
        series = series_for(rows, endpoint)
        endpoints[endpoint] = to_json(series, compare_pairs(series, COMPARISONS))
    return {
        "provenance": provenance,
        "completion_command": command,
        "comparisons": [list(item) for item in COMPARISONS],
        "endpoints": endpoints,
    }


def markdown(report: Mapping[str, object]) -> str:
    lines = ["# A1/A2/A3 paired analysis", ""]
    provenance = report.get("provenance")
    lines.append(f"Agent provenance: `{provenance}`")
    if provenance != "completion-command":
        lines.extend(
            [
                "",
                "> **这不是模型结果。** 该文件验证同 seed 配对、编码和报告链路，"
                "不能用于裁决 P1 或知识消融。",
            ]
        )
    lines.append("")
    for endpoint, payload in report["endpoints"].items():
        assert isinstance(payload, Mapping)
        lines.extend(
            [
                f"## {endpoint}",
                "",
                "| strategy | n | mean | median | stdev | 95% CI |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        distributions = payload["distributions"]
        assert isinstance(distributions, Mapping)
        for strategy, values in distributions.items():
            assert isinstance(values, Mapping)
            lines.append(
                f"| {strategy} | {values['n']} | {values['mean']:.6f} | "
                f"{values['median']:.6f} | {values['stdev']:.6f} | "
                f"[{values['ci_low']:.6f}, {values['ci_high']:.6f}] |"
            )
        lines.extend(["", "### Paired comparisons", ""])
        for item in payload["comparisons"]:
            assert isinstance(item, Mapping)
            lines.append(
                f"- `{item['name']}`: mean diff={item['mean_difference']:.6f}, "
                f"Holm p={item['adjusted_p']:.6g}, rejected={item['rejected']}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    document = json.loads(args.input.read_text(encoding="utf-8"))
    report = analyze(document)
    out = args.out or args.input.with_name(args.input.stem + ".analysis.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report) + "\n", encoding="utf-8")
    print(out)
    print(out.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
