"""A1/A2/A3 共用的最终 aliased 判定规则。

报告阶段不调用模型：模型只决定预算如何花，最终判定由所有 Agent 行共享的确定性
规则完成。这样 A1/A2/A3 的报告机制是同一个常量，provider 的输出格式不会污染
知识消融比较。
"""

from __future__ import annotations

from collections.abc import Sequence

REPEAT_FLOOR = 2


def declarations(
    prefixes: Sequence[str],
    pulls: Sequence[int],
    replies: Sequence[float],
) -> list[tuple[str, bool]]:
    """一个前缀至少探两次且一次未漏，才声明 aliased；未探过则不声明。"""

    if len(prefixes) != len(pulls) or len(prefixes) != len(replies):
        raise ValueError(
            f"prefixes({len(prefixes)}) / pulls({len(pulls)}) / "
            f"replies({len(replies)}) 长度不一致"
        )
    out: list[tuple[str, bool]] = []
    for index, prefix in enumerate(prefixes):
        if pulls[index] == 0:
            continue
        out.append(
            (
                prefix,
                pulls[index] >= REPEAT_FLOOR and replies[index] == pulls[index],
            )
        )
    return out


__all__ = ["REPEAT_FLOOR", "declarations"]
