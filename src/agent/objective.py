"""Agent 可观察的在线目标（T12）。

真值 `info_gain` 需要 oracle，策略侧看不到。本模块给 Agent 一个仅由 `Response`
计算的代理量：

1. 先按前缀统计探测数、回包数和响应签名；
2. 对“探够至少 5 次、回包率 ≥ 80%、且只出现一个来源身份”的前缀标记为
   饱和（saturated）；
3. 只用未饱和前缀的响应签名计算 Hill number
   `exp(ShannonEntropy(counts))`；
4. 该 Hill number 就是在线价值，`log2(Hill)` 是它的信息量诊断。

这个代理量与 `info_gain` 的关系是**方向性而非等值**：新出现的可信签名提高
代理量，重复的单一饱和来源不提高。它不读取真值，也不声称无偏估计
`I(Probes; Routers)`；论文必须把这句话写明。
"""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..adapter.types import Response

MIN_SATURATION_PROBES = 5
SATURATION_REPLY_RATE = 0.8


class ObjectiveError(ValueError):
    """在线目标的输入不合法。"""


@dataclass(frozen=True, slots=True)
class ObjectiveSnapshot:
    probes: int
    replies: int
    credible_prefixes: tuple[str, ...]
    saturated_prefixes: tuple[str, ...]
    unique_signatures: int
    effective_signatures: float
    online_info_bits: float
    duplicate_reply_rate: float


@dataclass(frozen=True, slots=True)
class _PrefixStats:
    probes: int = 0
    replies: int = 0
    sources: frozenset[str] = frozenset()
    signatures: Mapping[tuple[str, int | None, int | None, int | None], int] = field(
        default_factory=dict
    )


class OnlineObjective:
    """不可变累加器；每次 `observe()` 返回新对象。"""

    def __init__(
        self,
        prefixes: Iterable[str],
        *,
        stats: Mapping[str, _PrefixStats] | None = None,
    ) -> None:
        networks = tuple(
            sorted(
                (
                    ipaddress.ip_network(prefix, strict=False)
                    for prefix in prefixes
                ),
                key=lambda network: (network.prefixlen, int(network.network_address)),
            )
        )
        if not networks:
            raise ObjectiveError("在线目标至少需要一个前缀")
        self._networks = networks
        self._stats = dict(stats or {str(net): _PrefixStats() for net in networks})
        missing = {str(net) for net in networks} - set(self._stats)
        if missing:
            raise ObjectiveError(f"初始统计缺少前缀：{sorted(missing)}")

    @property
    def prefixes(self) -> tuple[str, ...]:
        return tuple(str(net) for net in self._networks)

    def prefix_for(self, address: str) -> str:
        try:
            addr = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ObjectiveError(f"{address!r} 不是合法地址") from exc
        matches = [net for net in self._networks if addr in net]
        if not matches:
            raise ObjectiveError(f"{address} 不在任务前缀内")
        return str(max(matches, key=lambda net: net.prefixlen))

    def observe(self, response: Response) -> OnlineObjective:
        if not isinstance(response, Response):
            raise ObjectiveError(f"在线目标只接受 Response，得到 {type(response).__name__}")
        prefix = self.prefix_for(response.dst)
        old = self._stats[prefix]
        probes = old.probes + 1
        replies = old.replies
        sources = set(old.sources)
        signatures = dict(old.signatures)
        if response.status == "received":
            replies += 1
            # Stage 1 的 Echo Reply 由靶场伪造 src=dst；此时不能把每个地址当成
            # 新来源，否则 aliased 前缀会制造虚假的无限多样性。
            source_identity = response.src if response.indirect else prefix
            if source_identity is None:
                raise ObjectiveError("received 响应缺少 src")
            sources.add(source_identity)
            signature = (
                source_identity,
                response.icmp_type,
                response.icmp_code,
                response.hop_limit,
            )
            signatures[signature] = signatures.get(signature, 0) + 1
        stats = dict(self._stats)
        stats[prefix] = _PrefixStats(
            probes=probes,
            replies=replies,
            sources=frozenset(sources),
            signatures=signatures,
        )
        return OnlineObjective(self.prefixes, stats=stats)

    def observe_many(self, responses: Iterable[Response]) -> OnlineObjective:
        objective = self
        for response in responses:
            objective = objective.observe(response)
        return objective

    def _saturated(self, stats: _PrefixStats) -> bool:
        return (
            stats.probes >= MIN_SATURATION_PROBES
            and stats.replies / stats.probes >= SATURATION_REPLY_RATE
            and len(stats.sources) <= 1
        )

    def snapshot(self) -> ObjectiveSnapshot:
        credible: list[str] = []
        saturated: list[str] = []
        credible_counts: list[int] = []
        total_replies = 0
        total_signatures = 0
        for prefix, stats in sorted(self._stats.items()):
            total_replies += stats.replies
            total_signatures += len(stats.signatures)
            if self._saturated(stats):
                saturated.append(prefix)
                continue
            if stats.replies:
                credible.append(prefix)
                credible_counts.extend(stats.signatures.values())

        if credible_counts:
            total = sum(credible_counts)
            probabilities = [count / total for count in credible_counts]
            entropy = -sum(p * math.log(p) for p in probabilities if p > 0.0)
            effective = math.exp(entropy)
        else:
            effective = 0.0
        duplicate_rate = (
            (total_replies - total_signatures) / total_replies if total_replies else 0.0
        )
        return ObjectiveSnapshot(
            probes=sum(stats.probes for stats in self._stats.values()),
            replies=total_replies,
            credible_prefixes=tuple(credible),
            saturated_prefixes=tuple(saturated),
            unique_signatures=total_signatures,
            effective_signatures=effective,
            online_info_bits=math.log2(effective) if effective > 0.0 else 0.0,
            duplicate_reply_rate=duplicate_rate,
        )

    def render(self) -> str:
        snapshot = self.snapshot()
        return "\n".join(
            [
                f"在线价值（可信签名有效数）：{snapshot.effective_signatures:.3f}",
                f"在线信息量诊断：{snapshot.online_info_bits:.3f} bit",
                f"可信前缀：{len(snapshot.credible_prefixes)} / {len(self.prefixes)}",
                f"饱和前缀：{len(snapshot.saturated_prefixes)}",
                f"重复回包比例：{snapshot.duplicate_reply_rate:.3f}",
            ]
        )


__all__ = [
    "MIN_SATURATION_PROBES",
    "ObjectiveError",
    "ObjectiveSnapshot",
    "OnlineObjective",
    "SATURATION_REPLY_RATE",
]
