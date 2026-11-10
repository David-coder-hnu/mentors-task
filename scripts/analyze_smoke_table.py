"""Stage 1 矩阵 JSON → 分布、配对检验、Holm 与共主端点判决。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.report import compare_pairs, summarize_distribution, to_json  # noqa: E402

B0 = "src.baselines.b0_probe_all:run"
B1 = "src.baselines.b1_uniform_random:run"
B4 = "src.baselines.b4_response_bandit:run"
M1 = "src.baselines.m1_memory_cold:run"
M2 = "src.baselines.m2_memory_warm:run"
STRATEGIES = (B0, B1, B4, M1, M2)
COMPARISONS = (("M2_vs_B4", M2, B4), ("M2_vs_M1", M2, M1))


def _rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = document.get("rows")
    if isinstance(payload, Mapping):
        payload = payload.get("rows")
    if not isinstance(payload, list):
        raise ValueError("输入里没有 rows 数组")
    return payload


def series_for(rows: Sequence[Mapping[str, Any]], endpoint: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {strategy: {} for strategy in STRATEGIES}
    for row in rows:
        strategy = row.get("strategy")
        if strategy not in out:
            continue
        seed = row.get("seed")
        value = row.get(endpoint)
        if not isinstance(seed, int) or not isinstance(value, (int, float)):
            continue
        out[strategy][seed] = float(value)
    for strategy in STRATEGIES:
        if not out[strategy]:
            raise ValueError(f"{strategy} 没有 {endpoint} 数据")
    return out


def _direction(left_right_mean: float, endpoint: str) -> str:
    if abs(left_right_mean) < 1e-15:
        return "tie"
    if endpoint == "aliased_waste_ratio":
        return "left" if left_right_mean < 0 else "right"
    return "left" if left_right_mean > 0 else "right"


def co_primary_decision(
    comparison_name: str,
    analyses: Mapping[str, Sequence[Any]],
    *,
    admissible: Mapping[str, bool],
) -> dict[str, str]:
    """按预注册 Pareto 规则判 A/B。`left=M2`。"""

    endpoint_results: dict[str, Any] = {}
    for endpoint, results in analyses.items():
        match = next((item for item in results if item.name == comparison_name), None)
        if match is None:
            raise ValueError(f"缺少比较 {comparison_name}/{endpoint}")
        endpoint_results[endpoint] = match

    _, left, right = next(item for item in COMPARISONS if item[0] == comparison_name)
    if not admissible.get(left, False) or not admissible.get(right, False):
        return {"decision": "invalid", "reason": "有 run 的 aliased 端点不可进入判据"}

    directions: dict[str, str] = {}
    significant: set[str] = set()
    for endpoint, match in endpoint_results.items():
        directions[endpoint] = _direction(match.test.mean_difference, endpoint)
        if match.holm.rejected:
            significant.add(directions[endpoint])

    left_better = "left" in significant
    right_better = "right" in significant
    if left_better and right_better:
        return {"decision": "inconclusive", "reason": "两个共主端点方向冲突"}
    if left_better:
        return {"decision": "left", "reason": "M2 至少一个端点显著更好且另一个不劣"}
    if right_better:
        return {"decision": "right", "reason": "对照至少一个端点显著更好且另一个不劣"}
    return {"decision": "tie", "reason": "两个共主端点均无显著差异"}


def analyze(document: Mapping[str, Any]) -> dict[str, Any]:
    rows = _rows(document)
    admissible: dict[str, bool] = {strategy: True for strategy in STRATEGIES}
    for row in rows:
        strategy = row.get("strategy")
        if strategy in admissible:
            admissible[strategy] &= bool(row.get("aliased_endpoint_admissible", False))

    endpoints = ("aliased_waste_ratio", "aliased_caught")
    analyses = {}
    for endpoint in endpoints:
        series = series_for(rows, endpoint)
        analyses[endpoint] = compare_pairs(series, COMPARISONS)

    decisions = {
        name: co_primary_decision(name, analyses, admissible=admissible)
        for name, _, _ in COMPARISONS
    }
    return {
        "admissible": admissible,
        "endpoints": {
            endpoint: to_json(series_for(rows, endpoint), comparisons)
            for endpoint, comparisons in analyses.items()
        },
        "decisions": decisions,
    }


def markdown(report: Mapping[str, Any]) -> str:
    lines = ["# Stage 1 analysis", "", "## Decisions", ""]
    for name, decision in report["decisions"].items():
        lines.append(f"- `{name}`: **{decision['decision']}** — {decision['reason']}")
    lines.extend(["", "## Distributions", ""])
    for endpoint, payload in report["endpoints"].items():
        lines.extend(
            [
                f"### {endpoint}",
                "",
                "| strategy | n | mean | median | stdev | min | max |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for strategy, dist in payload["distributions"].items():
            lines.append(
                f"| {strategy} | {dist['n']} | {dist['mean']:.6f} | "
                f"{dist['median']:.6f} | {dist['stdev']:.6f} | "
                f"{dist['minimum']:.6f} | {dist['maximum']:.6f} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
