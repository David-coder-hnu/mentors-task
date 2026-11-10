"""M1 —— 记忆族冷启动：每趟都从空观测计数开始。"""

from __future__ import annotations

from .memory_bandit import _Surface, run_memory_bandit


def run(surface: _Surface) -> None:
    run_memory_bandit(surface, use_memory=False)


__all__ = ["run"]
