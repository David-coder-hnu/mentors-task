"""Runtime-composed probing primitives for A2/A3 and B5-open.

The model authors a small ordered program, not arbitrary Python. A trusted
interpreter validates and executes the program against the current observations.
This is an open compositional action space: primitive programs are not required
to exist in the initial tool menu.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROGRAM_TARGETS = frozenset(
    {
        "balanced",
        "least_probed",
        "unprobed",
        "responsive",
    }
)
MAX_PROGRAM_STEPS = 4


class PrimitiveError(ValueError):
    """A constructed primitive is malformed or cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class ProgramStep:
    target: str
    fraction: float


def parse_steps(raw: object) -> tuple[ProgramStep, ...]:
    if not isinstance(raw, list) or not raw:
        raise PrimitiveError("program.steps 必须是非空数组")
    if len(raw) > MAX_PROGRAM_STEPS:
        raise PrimitiveError(f"program.steps 最多 {MAX_PROGRAM_STEPS} 步")
    steps: list[ProgramStep] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"target", "fraction"}:
            raise PrimitiveError(
                f"program.steps[{index}] 必须恰好包含 target / fraction"
            )
        target = item["target"]
        fraction = item["fraction"]
        if target not in PROGRAM_TARGETS:
            raise PrimitiveError(
                f"program.steps[{index}].target 必须是 {sorted(PROGRAM_TARGETS)} 之一"
            )
        if (
            not isinstance(fraction, (int, float))
            or isinstance(fraction, bool)
            or not 0.0 < float(fraction) <= 1.0
        ):
            raise PrimitiveError(
                f"program.steps[{index}].fraction 必须在 (0, 1]"
            )
        steps.append(ProgramStep(target=str(target), fraction=float(fraction)))
    total = sum(step.fraction for step in steps)
    if abs(total - 1.0) > 1e-9:
        raise PrimitiveError(f"program.steps 的 fraction 之和必须为 1，得到 {total}")
    return tuple(steps)


def _step_sizes(count: int, steps: Sequence[ProgramStep]) -> tuple[int, ...]:
    if count < 1:
        raise PrimitiveError("program count 必须是正整数")
    sizes = [int(count * step.fraction) for step in steps]
    sizes[-1] += count - sum(sizes)
    if any(size < 0 for size in sizes):
        raise PrimitiveError("program fraction 产生了负分配")
    return tuple(sizes)


def compose_counts(
    *,
    prefixes: Sequence[str],
    pulls: Mapping[str, int],
    replies: Mapping[str, int],
    steps: Sequence[ProgramStep],
    count: int,
) -> tuple[int, ...]:
    """Compile an ordered program into exact per-prefix sample counts.

    Each step consumes its declared share of the epoch. Within a step:

    - ``balanced``: round-robin over the whole task;
    - ``least_probed``: prefer the smallest real pull count;
    - ``unprobed``: prefer prefixes with zero pulls;
    - ``responsive``: prefer the highest Beta(1,1)-smoothed reply rate.
    """

    if not prefixes:
        raise PrimitiveError("没有前缀就没有可执行程序")
    observed = {prefix: int(pulls.get(prefix, 0)) for prefix in prefixes}
    rewarded = {prefix: int(replies.get(prefix, 0)) for prefix in prefixes}
    planned = [0] * len(prefixes)

    for step, size in zip(steps, _step_sizes(count, steps), strict=True):
        for _ in range(size):
            if step.target == "balanced":
                key = lambda i: (planned[i], i)
            elif step.target == "least_probed":
                key = lambda i: (observed[prefixes[i]], planned[i], i)
            elif step.target == "unprobed":
                key = lambda i: (
                    0 if observed[prefixes[i]] == 0 else 1,
                    observed[prefixes[i]],
                    planned[i],
                    i,
                )
            elif step.target == "responsive":
                key = lambda i: (
                    -(
                        (rewarded[prefixes[i]] + 1)
                        / (observed[prefixes[i]] + 2)
                    ),
                    planned[i],
                    i,
                )
            else:  # pragma: no cover - parse_steps already closes the set
                raise PrimitiveError(f"未知 target: {step.target}")
            index = min(range(len(prefixes)), key=key)
            planned[index] += 1
    if sum(planned) != count:
        raise PrimitiveError(f"program 分配 {sum(planned)} 次，预算要求 {count}")
    return tuple(planned)


def program_to_json(steps: Sequence[ProgramStep]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"target": step.target, "fraction": step.fraction} for step in steps
    )


__all__ = [
    "MAX_PROGRAM_STEPS",
    "PROGRAM_TARGETS",
    "PrimitiveError",
    "ProgramStep",
    "compose_counts",
    "parse_steps",
    "program_to_json",
]
