"""runner 侧的 LLM 决策代理。

模型调用与决策缓存都不能住在策略进程里：前者会打开网络，后者包含别的策略的
决策。A1/A2 只把规范化后的观测、工具池和渲染后的 prompt 经 stdio 交给本模块；
本模块在 runner 侧查缓存、调用 completion，再只把模型原文送回策略进程。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.cache import DecisionCache
from ..agent.decide import require_llm_identity

Completion = Callable[[str], str]


class DecisionBrokerError(RuntimeError):
    """模型或缓存层失败；不得降级成一次空决策。"""


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    raw: str
    cache_hit: bool


class DecisionBroker:
    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        seed: int,
        cache: DecisionCache,
        completion: Completion | None,
        protected_paths: Sequence[str | Path] = (),
    ) -> None:
        require_llm_identity(config)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError(f"seed 必须是整数，得到 {seed!r}")
        self.config = dict(config)
        self.seed = seed
        self.cache = cache
        self.completion = completion
        self.protected_paths = tuple(str(Path(path).resolve()) for path in protected_paths)

    def decide(
        self,
        *,
        prompt: str,
        state: Mapping[str, Any],
        tool_pool: Mapping[str, str],
    ) -> BrokerDecision:
        from ..agent.cache import CacheKey

        def tuple_lists(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: tuple_lists(item) for key, item in value.items()}
            if isinstance(value, list):
                return tuple(tuple_lists(item) for item in value)
            if isinstance(value, tuple):
                return tuple(tuple_lists(item) for item in value)
            return value

        key = CacheKey.of(
            state=tuple_lists(state),
            seed=self.seed,
            prompt=prompt,
            tool_pool=tool_pool,
            config=self.config,
        )
        raw = self.cache.lookup(key)
        hit = raw is not None
        if raw is None:
            if self.completion is None:
                raise DecisionBrokerError(
                    "决策缓存未命中，且 runner 没有配置 completion。"
                    "回放模式必须逐决策命中；这里不会偷偷联网。"
                )
            raw = self.completion(prompt)
            if not isinstance(raw, str) or not raw.strip():
                raise DecisionBrokerError("completion 必须返回非空字符串")
            self.cache.store(key, raw)
        return BrokerDecision(raw=raw, cache_hit=hit)


def command_completion(
    command: Sequence[str],
    *,
    timeout_s: float = 120.0,
) -> Completion:
    """把 prompt 写到子命令 stdin，把 stdout 当作模型原文。

    命令显式给出，不经 shell 展开。凭据由该命令自己的安全环境提供，本仓库
    不读取、不记录任何 API key。
    """

    argv = tuple(command)
    if not argv:
        raise ValueError("completion command 不能为空")
    if timeout_s <= 0:
        raise ValueError("completion timeout 必须为正数")

    def complete(prompt: str) -> str:
        try:
            result = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DecisionBrokerError(
                f"completion command 超过 {timeout_s:g}s 上限，已终止"
            ) from exc
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-8:]
            raise DecisionBrokerError(
                f"completion command 退出码 {result.returncode}：\n"
                + "\n".join(tail)
            )
        output = result.stdout.strip()
        if not output:
            raise DecisionBrokerError("completion command 没有输出")
        return output

    return complete


__all__ = [
    "BrokerDecision",
    "Completion",
    "DecisionBroker",
    "DecisionBrokerError",
    "command_completion",
]
