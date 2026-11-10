"""跨 run 的**策略自观测记忆**（T6c / 协议 v3）。

记忆只存每个前缀的 `pulls` 与 `rewards`，两列都由该策略自己的 `Response`
历史累计而来。它不存真值、不存其他策略的决策，也不接受任意扩展字段。

这份限制不是洁癖：M2 的作用是检验“有历史观测”与“冷启动”的差，若记忆能
塞入 `_rate`、aliased 标签或别的策略结果，M2 就不是记忆族，而是另一条真值
通路。因此 runner 与策略侧都走同一套严格解码。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias, TypedDict


class MemoryEntry(TypedDict):
    pulls: int
    rewards: int


MemorySnapshot: TypeAlias = dict[str, MemoryEntry]
_ENTRY_FIELDS = frozenset({"pulls", "rewards"})


class MemoryError(ValueError):
    """记忆载荷不符合测试协议。"""


def normalize_memory(
    payload: Mapping[str, Any] | None,
    prefixes: Sequence[str],
) -> MemorySnapshot:
    """把线格式记忆规范化成“每个前缀一项”的快照。

    Raises:
        MemoryError: 前缀越界、字段多/少、计数非整数、负数，或
            `rewards > pulls`。这些形状都会让 UCB 的统计失去含义。
    """

    allowed = tuple(dict.fromkeys(prefixes))
    if not allowed:
        raise MemoryError("没有前缀就没有可规范化的记忆")
    allowed_set = set(allowed)
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise MemoryError(f"memory 必须是对象，得到 {type(payload).__name__}")

    unknown_prefixes = set(payload) - allowed_set
    if unknown_prefixes:
        raise MemoryError(f"memory 含任务范围外的前缀：{sorted(unknown_prefixes)}")

    out: MemorySnapshot = {}
    for prefix in allowed:
        raw = payload.get(prefix, {"pulls": 0, "rewards": 0})
        if not isinstance(raw, Mapping):
            raise MemoryError(f"{prefix} 的记忆必须是对象，得到 {type(raw).__name__}")
        if set(raw) != _ENTRY_FIELDS:
            raise MemoryError(
                f"{prefix} 的记忆字段必须是 {sorted(_ENTRY_FIELDS)}，得到 {sorted(raw)}"
            )
        pulls = raw["pulls"]
        rewards = raw["rewards"]
        for name, value in (("pulls", pulls), ("rewards", rewards)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MemoryError(f"{prefix}.{name} 必须是非负整数，得到 {value!r}")
        if rewards > pulls:
            raise MemoryError(
                f"{prefix} 的 rewards={rewards} 大于 pulls={pulls}——回包数不可能超过探测数"
            )
        out[prefix] = {"pulls": pulls, "rewards": rewards}
    return out


def memory_to_wire(memory: MemorySnapshot, prefixes: Sequence[str]) -> dict[str, MemoryEntry]:
    """按任务前缀顺序输出稳定的 JSON 对象。"""

    normalized = normalize_memory(memory, prefixes)
    return {prefix: dict(normalized[prefix]) for prefix in dict.fromkeys(prefixes)}


__all__ = ["MemoryEntry", "MemoryError", "MemorySnapshot", "memory_to_wire", "normalize_memory"]
