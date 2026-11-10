"""Stage 2 的可数量：真实路由器发现与路由器信息量。

Stage 1 的 Punching Bag 只回 Echo Reply，没有“最后一跳路由器”这一层。Stage 2
通过 `RouterShim` 合成间接响应，因此主端点从“有没有内容”变成“发现了多少个
不同的真实路由器实体”。

本模块只读 `RunRecord` 与 `Stage2Truth`，不参与策略进程。`router_entropy_bits`
是在线可观察响应来源的经验熵，作为 `I(Probes; Routers)` 的可读近似；它不是
独立同分布样本上的无偏插件估计。论文主判据仍用可数的 `true_routers`。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from ..adapter.truth import Stage2Truth
from .score import RunRecord, ScoringError


@dataclass(frozen=True, slots=True)
class Stage2Score:
    true_routers: int
    indirect_responses: int
    repeated_router_responses: int
    unknown_sources: int
    direct_responses: int
    router_entropy_bits: float
    discovered_router_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.true_routers < 0:
            raise ScoringError("true_routers 不能为负")
        if self.indirect_responses < 0:
            raise ScoringError("indirect_responses 不能为负")
        if self.repeated_router_responses < 0:
            raise ScoringError("repeated_router_responses 不能为负")
        if self.unknown_sources < 0:
            raise ScoringError("unknown_sources 不能为负")
        if self.direct_responses < 0:
            raise ScoringError("direct_responses 不能为负")
        if self.router_entropy_bits < 0:
            raise ScoringError("router_entropy_bits 不能为负")
        if self.true_routers != len(self.discovered_router_ids):
            raise ScoringError(
                "true_routers 必须等于 discovered_router_ids 的去重数量"
            )


def score_stage2(run: RunRecord, truth: Stage2Truth) -> Stage2Score:
    """按首次出现的真实来源计数路由器，别名前缀不会重复得分。"""

    if not isinstance(truth, Stage2Truth):
        raise ScoringError(
            f"Stage 2 评分需要 Stage2Truth，得到 {type(truth).__name__}"
        )
    known = truth.routers
    discovered: list[str] = []
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    indirect = 0
    repeated = 0
    unknown = 0
    direct = 0

    for response in run.probes:
        if response.status != "received":
            continue
        if not response.indirect or response.src is None:
            direct += 1
            continue
        indirect += 1
        if response.src not in known:
            unknown += 1
            continue
        counts[response.src] += 1
        if response.src in seen:
            repeated += 1
            continue
        seen.add(response.src)
        discovered.append(response.src)

    if counts:
        total = sum(counts.values())
        entropy = -sum(
            (count / total) * math.log2(count / total)
            for count in counts.values()
            if count
        )
        if abs(entropy) < 1e-15:
            entropy = 0.0
    else:
        entropy = 0.0

    return Stage2Score(
        true_routers=len(discovered),
        indirect_responses=indirect,
        repeated_router_responses=repeated,
        unknown_sources=unknown,
        direct_responses=direct,
        router_entropy_bits=entropy,
        discovered_router_ids=tuple(discovered),
    )


__all__ = ["Stage2Score", "score_stage2"]
