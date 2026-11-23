"""Stage 2 的上下文与 RouterShim 探测出口。

Stage 2 复用 Stage 1 的前缀与响应率分档，但由本模块在 runner 侧生成显式合成
拓扑。路由器地址使用独立于任务前缀的保留文档地址空间；每个非 aliased 前缀
拥有三个稳定路由器，aliased 前缀只拥有一个。沉默前缀没有任何可达路由器。

这个映射是测试床定义，不是对真实互联网拓扑的估计。它只用于在 Response 层
检验策略能否把“响应很多”与“真实实体很多”分开。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapter.ground_truth import stage1_truth_from_config
from ..adapter.router_shim import RouterRule, RouterShim
from ..adapter.truth import Stage2Truth
from .executors import ProbeExecutor

ROUTERS_PER_RESPONSIVE_PREFIX = 3
ROUTER_BASE = ipaddress.IPv6Address("2001:db8:ffff::")


@dataclass(frozen=True, slots=True)
class Stage2Context:
    truth: Stage2Truth
    prefixes: tuple[str, ...]
    budget: int
    epoch_size: int
    config: dict[str, Any]
    rules: tuple[RouterRule, ...]

    def executor(self, seed: int) -> ProbeExecutor:
        return RouterShim(self.rules, seed=seed)


def _router_address(slot: int) -> str:
    if slot < 0:
        raise ValueError(f"router slot 不能为负，得到 {slot}")
    return str(ROUTER_BASE + slot + 1)


def _tupleize(value: object) -> object:
    """JSON 数组进入缓存键前必须显式变成 tuple（list 会污染 on-policy 键）。"""

    if isinstance(value, dict):
        return {str(key): _tupleize(item) for key, item in value.items()}
    if isinstance(value, list):
        return tuple(_tupleize(item) for item in value)
    return value


def load_stage2_context(config_path: Path) -> Stage2Context:
    """读 Stage 1 config，并叠加一个不含真值的 Stage 2 运行配置。

    若给出的是 `config/stage2_config.json`，其 `base_config` 指向 Stage 1 的
    前缀规格。两个文件都参与决策缓存键，避免把同一 seed 下不同拓扑的决策混用。
    """

    path = Path(config_path)
    stage2_doc = json.loads(path.read_text(encoding="utf-8"))
    base_name = stage2_doc.get("base_config")
    if isinstance(base_name, str):
        base_path = (path.parent / base_name).resolve()
    else:
        base_path = path
    base_doc = json.loads(base_path.read_text(encoding="utf-8"))
    base_truth = stage1_truth_from_config(base_path)

    budget = int(stage2_doc.get("budget", base_doc["budget"]))
    epoch_size = int(stage2_doc.get("epoch_size", base_doc["epoch_size"]))
    if budget < 1:
        raise ValueError(f"Stage 2 budget 必须是正整数，得到 {budget}")
    if epoch_size < 1:
        raise ValueError(f"Stage 2 epoch_size 必须是正整数，得到 {epoch_size}")

    raw_subnets = base_doc.get("subnets")
    if not isinstance(raw_subnets, list) or not raw_subnets:
        raise ValueError("Stage 1 base config 没有 subnets")
    raw_include = stage2_doc.get("include_prefixes")
    if raw_include is None:
        included: set[str] | None = None
    else:
        if not isinstance(raw_include, list) or not raw_include:
            raise ValueError("include_prefixes 必须是非空数组")
        included = {
            str(ipaddress.ip_network(value, strict=False)) for value in raw_include
        }

    rules: list[RouterRule] = []
    slot = 0
    selected = []
    for entry in raw_subnets:
        if not isinstance(entry, dict) or "ipv6_prefix" not in entry:
            raise ValueError("subnet 不是带 ipv6_prefix 的对象")
        prefix = str(ipaddress.ip_network(entry["ipv6_prefix"], strict=False))
        if included is None or prefix in included:
            selected.append((prefix, entry))
    if included is not None:
        missing = included - {prefix for prefix, _entry in selected}
        if missing:
            raise ValueError(f"include_prefixes 中有未知前缀：{sorted(missing)}")
    if not selected:
        raise ValueError("include_prefixes 过滤后没有前缀")

    for index, (prefix, entry) in enumerate(selected):
        aliased = bool(base_truth.prefixes[prefix]["aliased"])
        rates = (
            float(entry.get("default_response_rate", 0.0)),
            float(entry.get("EUI_response_rate", 0.0)),
            float(entry.get("lower_response_rate", 0.0)),
            float(entry.get("higher_response_rate", 0.0)),
        )
        response_rate = max(rates)
        if aliased:
            routers = (_router_address(slot),)
            slot += 1
        elif response_rate > 0.0:
            routers = tuple(
                _router_address(slot + offset)
                for offset in range(ROUTERS_PER_RESPONSIVE_PREFIX)
            )
            slot += ROUTERS_PER_RESPONSIVE_PREFIX
        else:
            routers = ()
        rules.append(
            RouterRule(
                prefix,
                routers,
                aliased=aliased,
                hop_limit=64 - (index % 4),
                response_rate=response_rate,
            )
        )

    shim = RouterShim(rules)
    prefixed_truth = shim.ground_truth()
    expected_aliased = {
        prefix: base_truth.prefixes[prefix]["aliased"] for prefix in prefixed_truth.prefixes
    }
    observed_aliased = {
        prefix: fields["aliased"] for prefix, fields in prefixed_truth.prefixes.items()
    }
    if observed_aliased != expected_aliased:
        raise ValueError("RouterShim 与 Stage 1 真值的 aliased 集合不一致")

    return Stage2Context(
        truth=prefixed_truth,
        prefixes=tuple(rule.prefix for rule in rules),
        budget=budget,
        epoch_size=epoch_size,
        config=_tupleize({**stage2_doc, "base_config_document": base_doc}),
        rules=tuple(rules),
    )


__all__ = [
    "ROUTERS_PER_RESPONSIVE_PREFIX",
    "ROUTER_BASE",
    "Stage2Context",
    "load_stage2_context",
]
