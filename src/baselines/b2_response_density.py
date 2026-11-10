"""B2 —— 响应密度贪心（Stage 2）。

每个 epoch 优先探“当前观测响应率最高”的前缀；未探测前缀先各探一次。
没有 UCB 探索奖励，因此它比 B4 更直接地追逐表面高响应区域。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Protocol

from .b4_response_bandit import Answered, declarations, round_sizes


class _Surface(Protocol):
    prefixes: tuple[str, ...]
    epoch_size: int

    @property
    def budget_left(self) -> int | None: ...

    def sample(self, prefix: str, n: int) -> list[Answered]: ...
    def report(self, prefix: str, aliased: bool) -> None: ...
    def rng(self) -> random.Random: ...


def plan_round(
    pulls: Sequence[int],
    replies: Sequence[float],
    round_size: int,
    rng: random.Random,
) -> tuple[int, ...]:
    """按经验响应率分配一轮；未探过的臂先覆盖。"""

    if len(pulls) != len(replies) or not pulls:
        raise ValueError("pulls/replies 必须等长且非空")
    if round_size < 1:
        raise ValueError(f"round_size 至少为 1，得到 {round_size}")
    planned_pulls = list(pulls)
    planned_replies = [float(value) for value in replies]
    plan = [0] * len(planned_pulls)

    for _ in range(round_size):
        densities = [
            math.inf if planned_pulls[index] == 0 else planned_replies[index] / planned_pulls[index]
            for index in range(len(planned_pulls))
        ]
        best = max(densities)
        ties = [index for index, value in enumerate(densities) if value == best]
        pick = ties[0] if len(ties) == 1 else ties[int(rng.random() * len(ties))]
        mean = (
            0.0
            if planned_pulls[pick] == 0
            else planned_replies[pick] / planned_pulls[pick]
        )
        plan[pick] += 1
        planned_pulls[pick] += 1
        # 虚拟拉杆：假设该前缀继续维持当前响应率。
        planned_replies[pick] += mean
    return tuple(plan)


def run(surface: _Surface) -> None:
    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None")
    prefixes = surface.prefixes
    if not prefixes:
        raise ValueError("没有前缀")
    epoch_size = surface.epoch_size
    if epoch_size < 1 or budget < epoch_size:
        raise ValueError(f"预算 {budget} 盖不住 epoch_size={epoch_size}")

    pulls = [0] * len(prefixes)
    replies = [0] * len(prefixes)
    rng = surface.rng()
    for size in round_sizes(budget, budget // epoch_size):
        plan = plan_round(pulls, replies, size, rng)
        for index, count in enumerate(plan):
            if count == 0:
                continue
            responses = surface.sample(prefixes[index], count)
            if len(responses) != count:
                raise ValueError(f"{prefixes[index]} 要 {count} 个响应，回来 {len(responses)} 个")
            pulls[index] += count
            replies[index] += sum(1 for response in responses if response.status == "received")

    for prefix, aliased in declarations(prefixes, pulls, replies):
        surface.report(prefix, aliased)


__all__ = ["plan_round", "run"]
