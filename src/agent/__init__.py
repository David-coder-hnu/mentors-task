"""Agent 决策层 —— **跑在策略进程里**（`strategy_child.py` 加载它）。

## 本包在 Stage 1 的交付物：两样，都不是数字

v6(a)（设计方案 `:567`）把 A1/A2 挡在 Stage 1 门外，所以 T5 在 Stage 1 交付的是：

1. **决策缓存**（`cache.py`）—— 键含 state / seed / prompt / tool_pool / config 的
   内容摘要，跨进程稳定；
2. **状态规范化**（`canonical.py` + `decide.Stage1State`）—— 与回包到达顺序无关。

此外 T12 交付 `objective.OnlineObjective`：一个只由 `Response` 计算、供 A1/A2
在 Stage 2 使用的在线代理量。它不读取真值，也不进入 Stage 1 冒烟表。

`decide.py` 里的 LLM 调用**能跑通单测，但不产生任何进入冒烟表的数字**。
Stage 2 原样复用的是缓存与规范化，**不是决策质量**。

## 硬约束（违反则整组实验作废，D7）

**本包的一切都跑在策略进程里，所以它的导入图里不得出现真值侧的任何模块。**

| 不得导入 | 为什么 |
|---|---|
| `src.adapter.oracle` / `src.adapter.truth` | 它们是真值的定义处；拿到就等于看到答案 |
| `src.adapter.runner` / `src.adapter.probe_pool` | runner 侧；策略发包走 `ProbeSurface`，不自己开 socket |
| `src.adapter.iface` | 原始套接字那一层 |
| `src.scoring.*` | **它必须有真值**（见 `src/scoring/__init__.py`），导入它等于绕过上面全部 |

最后一条最容易踩：`src/scoring/` 里有一个现成的前缀匹配、现成的状态归一化，
"顺手复用"看起来完全合理。**所以 `decide.Stage1State.prefix_for()` 自带了一份
最长前缀匹配、只用 `ipaddress`** —— 它不是重复代码，它是 D7 的代价。

这条约束**有自动验证**：`tests/unit/test_isolation.py` 在干净子进程里导入本包，
断言 `sys.modules` 里没有上面那张表的模块。判据 6 / T17 的四条不可失败项之一。

## 决策缓存是 runner 侧的能力（协议 v4，已接线）

设计方案的数据流图把**决策缓存画在 runner 进程的方框里**，同一张图的第 ③ 条边
写着「策略进程不得读决策缓存文件本身——缓存里存着别的策略的决策」。

于是本包提供的 `DecisionCache` 是**能力**，不是**许可**：A1/A2 在策略进程里
调用 `ProbeSurface.decide()`，请求经协议 v4 到 runner 的 `DecisionBroker`；缓存
查询与 completion 都发生在 runner 侧。策略进程只收到模型原文，不接触缓存文件。
"""

from __future__ import annotations

from .cache import (
    CACHE_FORMAT,
    KEY_COMPONENTS,
    CacheFormatError,
    CacheKey,
    CacheStats,
    DecisionCache,
    KeyMaterialError,
    cache_fingerprint,
)
from .ablation import AblatedResponse, AblationError, OpaqueEncoding, same_experiment_fields
from .canonical import KEY_SCHEME, CanonicalError, canonical, digest, digest_of
from .decide import (
    LLM_IDENTITY_KEYS,
    Action,
    ActionKind,
    Completion,
    Decision,
    DecisionError,
    DecisionSource,
    MalformedDecision,
    Stage1State,
    Tool,
    ToolPool,
    decide,
    parse_action,
    require_llm_identity,
)
from .prompts import DECIDER_TEMPLATE, render
from .objective import ObjectiveError, ObjectiveSnapshot, OnlineObjective
from .strategies import run_a1, run_a2, run_a3

__all__ = [
    "CACHE_FORMAT",
    "DECIDER_TEMPLATE",
    "KEY_COMPONENTS",
    "KEY_SCHEME",
    "LLM_IDENTITY_KEYS",
    "Action",
    "ActionKind",
    "AblatedResponse",
    "AblationError",
    "CacheFormatError",
    "CacheKey",
    "CacheStats",
    "CanonicalError",
    "Completion",
    "Decision",
    "DecisionCache",
    "DecisionError",
    "DecisionSource",
    "KeyMaterialError",
    "MalformedDecision",
    "ObjectiveError",
    "ObjectiveSnapshot",
    "OpaqueEncoding",
    "OnlineObjective",
    "Stage1State",
    "Tool",
    "ToolPool",
    "cache_fingerprint",
    "canonical",
    "decide",
    "digest",
    "digest_of",
    "parse_action",
    "render",
    "require_llm_identity",
    "same_experiment_fields",
    "run_a1",
    "run_a2",
    "run_a3",
]
