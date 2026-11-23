"""B5 —— 6SEVEN dealiasing + bit-reversal 发现分配器。

6SEVEN 的 online alias detection 是固定检测组件：每个唯一前缀在六个长度上
各探三个地址，命中两个即判 aliased。它本身不跨前缀分配预算。本基线保留该
检测规则，并把剩余预算交给确定性的 bit-reversal 发现分配器。

两阶段从同一个 `surface.budget_left` 扣费。来源分类见
`docs/protocols/b5_6seven_classification.md`。
"""

from __future__ import annotations

import ipaddress
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .b3_bit_reversal import address_at
from .b4_response_bandit import Answered

DETECTION_LENGTHS = (48, 64, 96, 112, 116, 120)
PROBES_PER_LENGTH = 3
REPLIES_TO_DECLARE = 2


class _Surface(Protocol):
    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def probe(self, dst: str, proto: str, hop_limit: int = 64) -> Answered: ...
    def report(self, prefix: str, aliased: bool) -> None: ...
    def rng(self) -> random.Random: ...


@dataclass(frozen=True, slots=True)
class AliasVerdict:
    prefix: str
    aliased: bool | None
    probes: int
    completed_lengths: tuple[int, ...]
    tested_targets: tuple[str, ...]
    replies: int = 0


def effective_lengths(prefix: str) -> tuple[int, ...]:
    prefix_len = ipaddress.ip_network(prefix, strict=False).prefixlen
    return tuple(length for length in DETECTION_LENGTHS if length >= prefix_len)


def _random_below(rng: random.Random, bits: int) -> int:
    """用多次 `random()` 拼出任意 bit 数，避免只用 53 bit 浮点精度。"""

    value = 0
    remaining = bits
    while remaining:
        chunk = min(26, remaining)
        value = (value << chunk) | int(rng.random() * (1 << chunk))
        remaining -= chunk
    return value


def targets_for_length(
    prefix: str,
    length: int,
    rng: random.Random,
) -> tuple[str, ...]:
    net = ipaddress.ip_network(prefix, strict=False)
    if length < net.prefixlen or length > net.max_prefixlen:
        raise ValueError(f"{length} 不在 {prefix} 的合法子前缀范围内")
    test_net = next(net.subnets(new_prefix=length))
    host_bits = test_net.max_prefixlen - length
    seen: set[ipaddress.IPv6Address] = set()
    while len(seen) < PROBES_PER_LENGTH:
        value = int(test_net.network_address) + _random_below(rng, host_bits)
        seen.add(ipaddress.IPv6Address(value))
    return tuple(str(address) for address in sorted(seen))


def detect_prefix(surface: _Surface, prefix: str, rng: random.Random) -> AliasVerdict:
    probes = 0
    replies_total = 0
    completed: list[int] = []
    tested: list[str] = []
    for length in effective_lengths(prefix):
        left = surface.budget_left
        if left is None:
            raise ValueError("surface.budget_left 是 None")
        if left < PROBES_PER_LENGTH:
            return AliasVerdict(
                prefix, None, probes, tuple(completed), tuple(tested), replies_total
            )
        replies = 0
        for target in targets_for_length(prefix, length, rng):
            tested.append(target)
            response = surface.probe(target, "icmp6-echo")
            probes += 1
            replies += int(response.status == "received")
        replies_total += replies
        completed.append(length)
        if replies >= REPLIES_TO_DECLARE:
            return AliasVerdict(
                prefix, True, probes, tuple(completed), tuple(tested), replies_total
            )
    return AliasVerdict(
        prefix, False, probes, tuple(completed), tuple(tested), replies_total
    )


def _discover(
    surface: _Surface,
    prefixes: Sequence[str],
    *,
    used: set[str],
) -> None:
    cursors = [0] * len(prefixes)
    while (left := surface.budget_left) is not None and left > 0:
        progressed = False
        for index, prefix in enumerate(prefixes):
            if surface.budget_left == 0:
                break
            while True:
                try:
                    target = address_at(prefix, cursors[index])
                except ValueError:
                    break
                cursors[index] += 1
                if target not in used:
                    used.add(target)
                    progressed = True
                    surface.probe(target, "icmp6-echo")
                    break
        if not progressed:
            break


def run(surface: _Surface) -> None:
    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None")
    prefixes = surface.prefixes
    if not prefixes:
        raise ValueError("没有前缀")

    rng = surface.rng()
    used: set[str] = set()
    verdicts: list[AliasVerdict] = []
    for prefix in prefixes:
        if surface.budget_left is None or surface.budget_left < PROBES_PER_LENGTH:
            break
        verdict = detect_prefix(surface, prefix, rng)
        verdicts.append(verdict)
        used.update(verdict.tested_targets)

    if surface.budget_left and surface.budget_left > 0:
        _discover(surface, prefixes, used=used)

    for verdict in verdicts:
        if verdict.aliased is not None:
            surface.report(verdict.prefix, verdict.aliased)


__all__ = [
    "AliasVerdict",
    "DETECTION_LENGTHS",
    "PROBES_PER_LENGTH",
    "REPLIES_TO_DECLARE",
    "detect_prefix",
    "effective_lengths",
    "run",
    "targets_for_length",
]
