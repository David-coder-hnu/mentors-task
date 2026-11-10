"""Stage 2 矩阵 → 路由器发现、aliased waste 的逐 seed 分布与配对检验。

默认比较 B5 对 B2/B4，只用于固定测试床内的基线诊断。A1/A2 尚未进入正式
结果时，本脚本不宣称完整的 P1 胜负。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.scoring.report import compare_pairs, summarize_distribution, to_json  # noqa: E402

B0 = "src.baselines.b0_probe_all:run"
B1 = "src.baselines.b1_uniform_random:run"
B2 = "src.baselines.b2_response_density:run"
B3 = "src.baselines.b3_bit_reversal:run"
B4 = "src.baselines.b4_response_bandit:run"
B5 = "src.baselines.b5_frp_aware:run"
STRATEGIES = (B0, B1, B2, B3, B4, B5)
DEFAULT_COMPARISONS = (("B5_vs_B2", B5, B2), ("B5_vs_B4", B5, B4))


def series_for(
    rows: Sequence[Mapping[str, object]],
    endpoint: str,
) -> dict[str, dict[int, float]]:
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
            raise ValueError(f"{strategy} 没有 {endpoint} 数据")
    return out


def analyze(
    document: Mapping[str, object],
    comparisons: Sequence[tuple[str, str, str]] = DEFAULT_COMPARISONS,
) -> dict[str, object]:
    raw_rows = document.get("rows")
    if isinstance(raw_rows, Mapping):
        raw_rows = raw_rows.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("输入没有 rows 数组")
    endpoints = {}
    for endpoint in ("true_routers", "aliased_waste_ratio"):
        series = series_for(raw_rows, endpoint)
        endpoints[endpoint] = to_json(series, compare_pairs(series, comparisons))
    return {"comparisons": [list(item) for item in comparisons], "endpoints": endpoints}


def markdown(report: Mapping[str, object]) -> str:
    lines = ["# Stage 2 analysis", ""]
    lines.extend(
        [
            "> RouterShim 合成测试床；结论只在这个 Response 层拓扑内成立。",
            "",
        ]
    )
    for endpoint, payload in report["endpoints"].items():
        assert isinstance(payload, Mapping)
        lines.extend(
            [
                f"## {endpoint}",
                "",
                "| strategy | n | mean | median | stdev | q25 | q75 | 95% CI |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        distributions = payload["distributions"]
        assert isinstance(distributions, Mapping)
        for strategy, values in distributions.items():
            assert isinstance(values, Mapping)
            lines.append(
                f"| {strategy} | {values['n']} | {values['mean']:.6f} | "
                f"{values['median']:.6f} | {values['stdev']:.6f} | "
                f"{values['q25']:.6f} | {values['q75']:.6f} | "
                f"[{values['ci_low']:.6f}, {values['ci_high']:.6f}] |"
            )
        lines.extend(["", "### Paired comparisons", ""])
        comparisons = payload["comparisons"]
        assert isinstance(comparisons, list)
        for item in comparisons:
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
