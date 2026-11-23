"""A3 的不透明同构 prompt：保留任务结构，不暴露编址语义。"""

from __future__ import annotations

from collections.abc import Sequence

ABLATED_DECIDER_TEMPLATE = """\
You are selecting regions in a finite-budget exploration task.

Some regions are saturated: every item responds, so they look productive but carry
no new information. All regions and nodes are opaque tokens generated for this run.

## available actions

{tools}

## observations

{state}

Return exactly one JSON object:

{actions}

Do not emit unknown keys. `reason` is required. Each action must spend the full
current epoch budget; final saturation labels are produced by a fixed shared rule.
"""


def render_ablated(
    *,
    tools: Sequence[tuple[str, str]],
    state_text: str,
    construction: bool = False,
) -> str:
    rendered_tools = "\n".join(f"- `{name}`: {description}" for name, description in tools)
    actions = [
        '  {"action": "probe", "region": "<opaque token>", "count": <positive integer>, "reason": "<short reason>"}'
    ]
    if construction:
        actions.append(
            '  {"action": "construct", "name": "<identifier>", '
            '"blueprint": "balanced_probe", "count": <positive integer>, '
            '"reason": "<short reason>"}'
        )
        actions.append(
            '  {"action": "compose", "name": "<identifier>", "steps": '
            '[{"target": "least_probed|unprobed|responsive|balanced", '
            '"fraction": <0..1>}], "count": <positive integer>, '
            '"reason": "<short reason>"}'
        )
    return ABLATED_DECIDER_TEMPLATE.format(
        tools=rendered_tools or "(empty)",
        state=state_text,
        actions="\n".join(actions),
    )


__all__ = ["ABLATED_DECIDER_TEMPLATE", "render_ablated"]
