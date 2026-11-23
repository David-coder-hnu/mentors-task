"""B5-open: hand-authored pre-run primitive for the P1 matrix.

B5 detection stays identical to the closed baseline. Discovery uses a primitive
constructed before evaluation from the same safe compositional DSL exposed to A2.
This is the fair human-authored control for "open action space".
"""

from __future__ import annotations

import random
from typing import Protocol

from ..agent.primitive import ProgramStep, compose_counts
from .b4_response_bandit import Answered
from .b5_frp_aware import AliasVerdict, detect_prefix

B5_OPEN_PROGRAM: tuple[ProgramStep, ...] = (
    ProgramStep("least_probed", 0.6),
    ProgramStep("responsive", 0.4),
)


class _Surface(Protocol):
    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def probe(self, dst: str, proto: str, hop_limit: int = 64) -> Answered: ...
    def sample(self, prefix: str, n: int) -> list[Answered]: ...
    def report(self, prefix: str, aliased: bool) -> None: ...
    def rng(self) -> random.Random: ...


def run(surface: _Surface) -> None:
    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None")
    prefixes = surface.prefixes
    if not prefixes:
        raise ValueError("没有前缀")

    rng = surface.rng()
    verdicts: list[AliasVerdict] = []
    for prefix in prefixes:
        if surface.budget_left is None or surface.budget_left < 3:
            break
        verdicts.append(detect_prefix(surface, prefix, rng))

    pulls = {prefix: 0 for prefix in prefixes}
    replies = {prefix: 0 for prefix in prefixes}
    for verdict in verdicts:
        pulls[verdict.prefix] += verdict.probes
        replies[verdict.prefix] += verdict.replies

    remaining = surface.budget_left
    if remaining is None:
        raise ValueError("surface.budget_left 是 None")
    if remaining > 0:
        counts = compose_counts(
            prefixes=prefixes,
            pulls=pulls,
            replies=replies,
            steps=B5_OPEN_PROGRAM,
            count=remaining,
        )
        for prefix, count in zip(prefixes, counts, strict=True):
            if count == 0:
                continue
            responses = surface.sample(prefix, count)
            if len(responses) != count:
                raise ValueError(f"{prefix} 要 {count} 个响应，回来 {len(responses)} 个")

    for verdict in verdicts:
        if verdict.aliased is not None:
            surface.report(verdict.prefix, verdict.aliased)


__all__ = ["B5_OPEN_PROGRAM", "run"]
