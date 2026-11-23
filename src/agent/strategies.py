"""A1/A2/A3 的 Stage 2 决策循环。

三条策略共享同一个模型、温度、预算、epoch 和动作 schema。A1 只有初始工具池；
A2 额外拥有受限的 `construct` 原语。这里的“构造”先落成经过白名单验证的
`balanced_probe` 复合动作，而不是执行任意 Python：策略进程的沙箱、预算账本和
Schema 仍各只有一条来源。它证明了开放动作空间在运行期可接线，但不把这一实现
夸大成任意代码执行能力。

最终 aliased 判定不调用模型，而由 A1/A2/A3 共用的 `declarations()` 固定规则产生。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from ..adapter.types import Response
from .ablation import OpaqueEncoding
from .decide import Action, Stage1State, Tool, ToolPool, parse_action
from .objective import OnlineObjective
from .primitive import compose_counts
from .primitive import parse_steps
from .prompts import render
from .reporting import declarations


class AgentSurface(Protocol):
    prefixes: tuple[str, ...]
    epoch_size: int

    @property
    def budget_left(self) -> int | None: ...

    def sample(self, prefix: str, n: int) -> list[Response]: ...
    def report(self, prefix: str, aliased: bool) -> None: ...
    def decide(
        self,
        *,
        prompt: str,
        state: Mapping[str, object],
        tool_pool: Mapping[str, str],
    ) -> tuple[str, bool]: ...


INITIAL_TOOLS = (
    Tool("sample", "在一个 region 中探测 count 个地址并返回全部 Response。"),
)

CONSTRUCT_TOOL = Tool(
    "construct",
    "构造并立即执行一个复合原语。当前唯一安全 blueprint 是 balanced_probe："
    "把 count 次探测优先分给累计探测数最少的 regions。",
)

COMPOSE_TOOL = Tool(
    "compose",
    "在运行时组合新的探测程序。steps 按顺序使用 balanced / least_probed / "
    "unprobed / responsive 目标，fraction 之和必须为 1。",
)

ABLATED_TOOLS = (
    Tool("sample", "Observe count items in one region."),
    Tool(
        "construct",
        "Create and immediately execute a balanced allocation across regions.",
    ),
    Tool(
        "compose",
        "Compose an ordered program from balanced, least-probed, unprobed, "
        "and responsive allocation steps.",
    ),
)


def _balanced_counts(
    pulls: Mapping[str, int],
    prefixes: tuple[str, ...],
    count: int,
) -> tuple[int, ...]:
    if count < 1:
        raise ValueError("balanced_probe count 必须是正整数")
    planned = dict(pulls)
    out = [0] * len(prefixes)
    for _ in range(count):
        index = min(
            range(len(prefixes)),
            key=lambda i: (planned.get(prefixes[i], 0), i),
        )
        out[index] += 1
        planned[prefixes[index]] = planned.get(prefixes[index], 0) + 1
    return tuple(out)


def _execute_construct(
    surface: AgentSurface,
    state: Stage1State,
    objective: OnlineObjective,
    action: Action,
) -> tuple[Stage1State, OnlineObjective]:
    if action.blueprint != "balanced_probe" or action.count is None:
        raise ValueError(f"不支持的构造动作：{action!r}")
    remaining = state.remaining_budget
    if action.count > remaining:
        raise ValueError(
            f"construct 要 {action.count} 次，但只剩 {remaining} 次预算；"
            "模型必须在动作前遵守预算"
        )
    counts = _balanced_counts(state.probes, state.prefixes, action.count)
    for prefix, count in zip(state.prefixes, counts):
        if count == 0:
            continue
        responses = surface.sample(prefix, count)
        if len(responses) != count:
            raise ValueError(f"{prefix} 要 {count} 个响应，回来 {len(responses)} 个")
        state = state.observe(responses)
        objective = objective.observe_many(responses)
        if surface.budget_left is not None:
            state = state.with_budget_left(surface.budget_left)
    return state, objective


def _execute_compose(
    surface: AgentSurface,
    state: Stage1State,
    objective: OnlineObjective,
    action: Action,
) -> tuple[Stage1State, OnlineObjective]:
    if action.count is None or action.steps is None:
        raise ValueError("compose 缺少 count 或 steps")
    expected = min(surface.epoch_size, state.remaining_budget)
    if action.count != expected:
        raise ValueError(
            f"本 epoch 必须恰好花 {expected} 次，constructed program 给了 {action.count} 次"
        )
    counts = compose_counts(
        prefixes=state.prefixes,
        pulls=state.probes,
        replies=state.replies,
        steps=action.steps,
        count=action.count,
    )
    for prefix, count in zip(state.prefixes, counts, strict=True):
        if count == 0:
            continue
        responses = surface.sample(prefix, count)
        if len(responses) != count:
            raise ValueError(f"{prefix} 要 {count} 个响应，回来 {len(responses)} 个")
        state = state.observe(responses)
        objective = objective.observe_many(responses)
        if surface.budget_left is not None:
            state = state.with_budget_left(surface.budget_left)
    return state, objective


def _parse_ablated_action(
    raw: str,
    *,
    encoding: OpaqueEncoding,
    expected_count: int,
) -> Action:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        raise ValueError(f"A3 决策不是 JSON 对象：{raw!r}")

    # Provider compatibility: a single program step is a complete one-step
    # compose program even when the outer action envelope is omitted.
    if set(payload) == {"target", "fraction"}:
        return Action(
            kind="compose",
            reason="A3 provider returned a step-only program",
            count=expected_count,
            name="opaque_single_step",
            # A one-step program necessarily spends the whole epoch, so a
            # provider-supplied partial fraction is normalized to 1.0.
            steps=parse_steps([{"target": payload["target"], "fraction": 1.0}]),
        )
    return encoding.translate_action_payload(payload)


def _run(
    surface: AgentSurface,
    *,
    tools: tuple[Tool, ...],
    allow_construct: bool,
    encoding: OpaqueEncoding | None = None,
) -> None:
    budget = surface.budget_left
    if budget is None:
        raise ValueError("surface.budget_left 是 None")
    pool = ToolPool(tools)
    state = Stage1State.initial(prefixes=surface.prefixes, budget=budget)
    objective = OnlineObjective(surface.prefixes)
    max_decisions = budget + 1

    for _ in range(max_decisions):
        if state.remaining_budget <= 0:
            break
        if encoding is None:
            prompt = render(
                tools=pool.as_pairs(),
                state_text=(
                    state.render()
                    + f"\n本 epoch 必须恰好花费：{min(surface.epoch_size, state.remaining_budget)}"
                ),
                objective_text=objective.render(),
                construction=allow_construct,
                reporting=False,
            )
        else:
            prompt = encoding.render_prompt(
                tools=pool.as_pairs(),
                state=state,
                construction=allow_construct,
                epoch_budget=min(surface.epoch_size, state.remaining_budget),
            )
        raw, _cache_hit = surface.decide(
            prompt=prompt,
            state=state.canonical_value(),
            tool_pool=pool.as_mapping(),
        )
        if encoding is None:
            action = parse_action(raw, origin="runner 决策代理")
        else:
            action = _parse_ablated_action(
                raw,
                encoding=encoding,
                expected_count=min(surface.epoch_size, state.remaining_budget),
            )
        state = state.next_epoch()

        if action.kind in {"stop", "report", "final_report"}:
            raise ValueError(
                f"预算还剩 {state.remaining_budget} 时收到 {action.kind}；"
                "探测阶段必须先花完预算"
            )

        if action.kind == "probe":
            assert action.prefix is not None and action.count is not None
            expected = min(surface.epoch_size, state.remaining_budget)
            if action.count != expected:
                raise ValueError(
                    f"本 epoch 必须恰好花 {expected} 次，模型给了 {action.count} 次"
                )
            responses = surface.sample(action.prefix, action.count)
            if len(responses) != action.count:
                raise ValueError(
                    f"{action.prefix} 要 {action.count} 个响应，回来 {len(responses)} 个"
                )
            state = state.observe(responses)
            objective = objective.observe_many(responses)
            if surface.budget_left is not None:
                state = state.with_budget_left(surface.budget_left)
        elif action.kind == "construct":
            if not allow_construct:
                raise ValueError("A1 的封闭工具池不允许 construct")
            expected = min(surface.epoch_size, state.remaining_budget)
            if action.count != expected:
                raise ValueError(
                    f"本 epoch 必须恰好花 {expected} 次，模型给了 {action.count} 次"
                )
            state, objective = _execute_construct(surface, state, objective, action)
        elif action.kind == "compose":
            if not allow_construct:
                raise ValueError("A1 的封闭工具池不允许 compose")
            state, objective = _execute_compose(surface, state, objective, action)
        else:  # pragma: no cover - Literal 已封口
            raise ValueError(f"未知动作 {action.kind}")

    if state.remaining_budget != 0:
        raise ValueError(
            f"A1/A2/A3 结束时还剩 {state.remaining_budget} 次预算；"
            "运行不完整，不能进入主表"
        )

    final = declarations(
        state.prefixes,
        [state.probes.get(prefix, 0) for prefix in state.prefixes],
        [state.replies.get(prefix, 0) for prefix in state.prefixes],
    )
    for prefix, aliased in final:
        state = state.with_report(prefix, aliased=aliased)
        surface.report(prefix, aliased)


def run_a1(surface: AgentSurface) -> None:
    _run(surface, tools=INITIAL_TOOLS, allow_construct=False)


def run_a2(surface: AgentSurface) -> None:
    _run(
        surface,
        tools=(*INITIAL_TOOLS, CONSTRUCT_TOOL, COMPOSE_TOOL),
        allow_construct=True,
    )


def run_a3(surface: AgentSurface) -> None:
    encoding = OpaqueEncoding(surface.prefixes)
    _run(
        surface,
        tools=ABLATED_TOOLS,
        allow_construct=True,
        encoding=encoding,
    )


__all__ = [
    "ABLATED_TOOLS",
    "COMPOSE_TOOL",
    "CONSTRUCT_TOOL",
    "INITIAL_TOOLS",
    "run_a1",
    "run_a2",
    "run_a3",
]
