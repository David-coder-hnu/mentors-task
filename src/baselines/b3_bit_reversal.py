"""B3 —— bit-reversal 拟随机基线（6Seeks Theorem 1）。"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from typing import Protocol

from .b4_response_bandit import Answered, declarations


class _Surface(Protocol):
    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def probe(self, dst: str, proto: str, hop_limit: int = 64) -> Answered: ...
    def report(self, prefix: str, aliased: bool) -> None: ...


def reverse_bits(value: int, width: int) -> int:
    if width < 0:
        raise ValueError("width 不能为负")
    if value < 0 or value >= 1 << width:
        raise ValueError(f"value={value} 超出 {width} bit")
    out = 0
    for _ in range(width):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


def address_at(prefix: str, cursor: int) -> str:
    """前缀内第 `cursor` 个 bit-reversal 主机地址。"""

    net = ipaddress.ip_network(prefix, strict=False)
    width = net.max_prefixlen - net.prefixlen
    ordinal = cursor + 1
    if cursor < 0 or ordinal >= 1 << width:
        raise ValueError(f"{prefix} 里没有第 {cursor} 个地址")
    offset = reverse_bits(ordinal, width)
    return str(net.network_address + offset)


def plan_targets(prefixes: Sequence[str], budget: int) -> tuple[tuple[str, str], ...]:
    """跨前缀轮转，并在每个前缀内按 bit reversal 前进。"""

    if not prefixes:
        raise ValueError("没有前缀")
    if budget < 1:
        raise ValueError("预算至少为 1")
    cursors = [0] * len(prefixes)
    out: list[tuple[str, str]] = []
    for step in range(budget):
        index = step % len(prefixes)
        prefix = prefixes[index]
        out.append((prefix, address_at(prefix, cursors[index])))
        cursors[index] += 1
    return tuple(out)


def run(surface: _Surface) -> None:
    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None")
    prefixes = surface.prefixes
    targets = plan_targets(prefixes, budget)
    pulls = [0] * len(prefixes)
    replies = [0] * len(prefixes)
    index_of = {prefix: index for index, prefix in enumerate(prefixes)}
    for prefix, address in targets:
        response = surface.probe(address, "icmp6-echo")
        index = index_of[prefix]
        pulls[index] += 1
        replies[index] += int(response.status == "received")

    for prefix, aliased in declarations(prefixes, pulls, replies):
        surface.report(prefix, aliased)


__all__ = ["address_at", "plan_targets", "reverse_bits", "run"]
