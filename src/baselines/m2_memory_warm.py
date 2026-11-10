"""M2 —— 记忆族有历史：读取本策略先前的 pulls/rewards，并在收尾回写。"""

from __future__ import annotations

from .memory_bandit import _Surface, run_memory_bandit


def run(surface: _Surface) -> None:
    run_memory_bandit(surface, use_memory=True)


__all__ = ["run"]
