"""生成 Stage 1 的留出集配置（T18 / D18）。

留出集不是把调参集的前缀换几个地址。它必须同时改变三类容易被过拟合的对象：

1. **前缀空间**：全部落在独立的 `2001:db8:a5::/48`；
2. **aliased 组成**：6 个 aliased，而调参集是 8 个；
3. **响应率分布**：各内容档与 hot 档都取不同数值。

预算、epoch、排空窗口等实验口径保持不变。这样策略面对的是同一项任务，而不是
一条被悄悄改短的预算，但调出来的阈值不能靠记住调参集的地址或档位作弊。

生成顺序由 `(seed, 原始位置, role)` 的 SHA-256 排序决定，不依赖 Python 内建
`hash()`，也不会受进程加盐影响。同 seed 重跑得到完全相同的 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any

from src.agent.decide import LLM_IDENTITY_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TUNING_CONFIG = REPO_ROOT / "config" / "stage1_config.json"
DEFAULT_OUTPUT = REPO_ROOT / "config" / "heldout" / "stage1_heldout.json"
DEFAULT_SEED = 20261109
HELDOUT_ROUTE = ipaddress.ip_network("2001:db8:a5::/48")
EXPECTED_PREFIXES = 32

#: 与调参集刻意不同的组成与速率。这里只放生成规则；实际 JSON 由本脚本产出。
ROLE_COUNTS: dict[str, int] = {
    "aliased": 6,
    "content-mid": 7,
    "content-low": 7,
    "content-sparse": 4,
    "tier-eui-hot": 2,
    "tier-byte-hot": 2,
    "silent": 4,
}

ROLE_RATES: dict[str, dict[str, float]] = {
    "aliased": {
        "default": 1.0,
        "EUI": 1.0,
        "lower": 1.0,
        "higher": 1.0,
    },
    "content-mid": {
        "default": 0.35,
        "EUI": 0.35,
        "lower": 0.35,
        "higher": 0.35,
    },
    "content-low": {
        "default": 0.08,
        "EUI": 0.08,
        "lower": 0.08,
        "higher": 0.08,
    },
    "content-sparse": {
        "default": 0.01,
        "EUI": 0.01,
        "lower": 0.01,
        "higher": 0.01,
    },
    "tier-eui-hot": {
        "default": 0.01,
        "EUI": 0.90,
        "lower": 0.01,
        "higher": 0.01,
    },
    "tier-byte-hot": {
        "default": 0.01,
        "EUI": 0.01,
        "lower": 0.65,
        "higher": 0.65,
    },
    "silent": {
        "default": 0.0,
        "EUI": 0.0,
        "lower": 0.0,
        "higher": 0.0,
    },
}

COMMON_KEYS = (
    "W_s",
    "epoch_size",
    "budget",
    "api_cost_cap",
    "wall_clock_cap_s",
    *LLM_IDENTITY_KEYS,
)


class HeldoutConfigError(ValueError):
    """输入或生成规则不满足留出集契约。"""


def _require_seed(seed: int) -> None:
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise HeldoutConfigError(f"seed 必须是非负整数，得到 {seed!r}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_role_order(seed: int, roles: list[str]) -> list[str]:
    """按内容哈希排序，避免 `hash()` 的进程级随机化和版本差异。"""

    return sorted(
        roles,
        key=lambda role: hashlib.sha256(f"{seed}\0{role}".encode("utf-8")).digest(),
    )


def _subnet(prefix: str, role: str) -> dict[str, Any]:
    rates = ROLE_RATES[role]
    return {
        "ipv6_prefix": prefix,
        "_role": role,
        "default_response_rate": rates["default"],
        "EUI_response_rate": rates["EUI"],
        "lower_response_rate": rates["lower"],
        "higher_response_rate": rates["higher"],
    }


def build_heldout_config(
    tuning_config: Path = DEFAULT_TUNING_CONFIG,
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """构造完整的留出集 config，但不写盘。"""

    _require_seed(seed)
    tuning_config = Path(tuning_config)
    with tuning_config.open("r", encoding="utf-8") as handle:
        tuning = json.load(handle)

    missing = [key for key in COMMON_KEYS if key not in tuning]
    if missing:
        raise HeldoutConfigError(
            f"调参集缺少不能随拓扑平移的实验口径：{', '.join(missing)}"
        )

    roles = [
        role
        for role, count in ROLE_COUNTS.items()
        for _ in range(count)
    ]
    if len(roles) != EXPECTED_PREFIXES:
        raise HeldoutConfigError(
            f"角色表应有 {EXPECTED_PREFIXES} 项，实际 {len(roles)}"
        )
    ordered_roles = _stable_role_order(seed, roles)

    subnets = [
        _subnet(f"2001:db8:a5:{index:x}::/64", role)
        for index, role in enumerate(ordered_roles)
    ]

    config: dict[str, Any] = {
        "_comment": [
            "Stage 1 留出集（T18）。这是独立调参用途的靶场真值，不参与主表。",
            "全部前缀位于独立 /48；aliased 数量与响应率分档均与调参集不同。",
            '生成入口：.venv/bin/python -m scripts.make_heldout',
            "禁止手工修改；改生成规则后重跑脚本与 tests/test_heldout_disjoint.py。",
        ],
        "config_role": "heldout",
        "topology_id": f"stage1-heldout-v1-seed-{seed}",
        "generator_seed": seed,
        "tuning_config_sha256": _sha256_file(tuning_config),
    }
    for key in COMMON_KEYS:
        config[key] = tuning[key]
    config["subnets"] = subnets
    return config


def render_config(config: dict[str, Any]) -> str:
    """稳定、可 diff 的 JSON 文本。"""

    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def write_heldout(
    output: Path = DEFAULT_OUTPUT,
    *,
    tuning_config: Path = DEFAULT_TUNING_CONFIG,
    seed: int = DEFAULT_SEED,
) -> Path:
    """生成并写入留出集，返回输出路径。"""

    output = Path(output)
    config = build_heldout_config(tuning_config, seed=seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_config(config), encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--tuning-config", type=Path, default=DEFAULT_TUNING_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    output = write_heldout(
        args.output,
        tuning_config=args.tuning_config,
        seed=args.seed,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
