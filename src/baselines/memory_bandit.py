"""M1/M2 共用的 UCB1 记忆底座（T6c）。

两行只在**是否装入跨 run 历史**这一点不同：

* M1：每趟从空计数开始，不读取也不回写历史；
* M2：握手读入本策略先前 run 的 `pulls/rewards`，本趟结束时显式回写。

策略算法、epoch 节奏、预算分配、声明规则与随机源完全相同。这样
`M2 vs M1` 只改变“有历史/无历史”，`M2 vs B4` 只改变“跨 run 记忆/无记忆”。
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Protocol

from .b4_response_bandit import Answered, declarations, plan_round, round_sizes


class _Surface(Protocol):
    prefixes: tuple[str, ...]
    epoch_size: int

    @property
    def budget_left(self) -> int | None: ...

    @property
    def memory(self) -> Mapping[str, Mapping[str, int]]: ...

    def sample(self, prefix: str, n: int) -> list[Answered]: ...
    def report(self, prefix: str, aliased: bool) -> None: ...
    def remember(self, memory: Mapping[str, object]) -> None: ...
    def rng(self) -> random.Random: ...


def _initial_counts(
    surface: _Surface,
    *,
    use_memory: bool,
) -> tuple[list[int], list[int]]:
    prefixes = surface.prefixes
    pulls = [0] * len(prefixes)
    rewards = [0] * len(prefixes)
    if not use_memory:
        return pulls, rewards

    memory = surface.memory
    for index, prefix in enumerate(prefixes):
        entry = memory.get(prefix)
        if entry is None:
            continue
        pulls[index] = int(entry["pulls"])
        rewards[index] = int(entry["rewards"])
    return pulls, rewards


def run_memory_bandit(surface: _Surface, *, use_memory: bool) -> None:
    """跑 M1/M2 的共同算法。

    Args:
        surface: 策略侧唯一允许看到的对象。
        use_memory: M2 为真；M1 为假。**没有第三个行为分支**——这是让
            两行只差一个自变量的结构性保证。
    """

    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None——握手没有送初始预算")
    prefixes = surface.prefixes
    if not prefixes:
        raise ValueError("没有前缀就没有臂")
    epoch_size = surface.epoch_size
    if not isinstance(epoch_size, int) or isinstance(epoch_size, bool) or epoch_size < 1:
        raise ValueError(f"epoch_size 必须是正整数，得到 {epoch_size!r}")
    if budget < epoch_size:
        raise ValueError(f"预算 {budget} 小于一个 epoch（{epoch_size}）")

    pulls, rewards = _initial_counts(surface, use_memory=use_memory)
    rounds = budget // epoch_size
    rng = surface.rng()

    for size in round_sizes(budget, rounds):
        plan = plan_round(pulls, rewards, size, rng)
        for index, count in enumerate(plan):
            if count == 0:
                continue
            responses = surface.sample(prefixes[index], count)
            if len(responses) != count:
                raise ValueError(
                    f"{prefixes[index]} 上要了 {count} 个响应，回来 {len(responses)} 个"
                )
            pulls[index] += count
            rewards[index] += sum(1 for response in responses if response.status == "received")

    for prefix, aliased in declarations(prefixes, pulls, rewards):
        surface.report(prefix, aliased)

    if use_memory:
        surface.remember(
            {
                prefix: {"pulls": pulls[index], "rewards": rewards[index]}
                for index, prefix in enumerate(prefixes)
            }
        )


__all__ = ["run_memory_bandit"]
