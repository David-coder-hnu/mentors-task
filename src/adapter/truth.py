"""真值类型 —— **runner 侧专有**（D7）。

## 为什么把真值从 `types.py` 里搬出来

原先 `Stage1Truth` 和 `Response` 同住在 `types.py`，而那与 `types.py` 自己的
docstring 直接矛盾——它写着「策略侧进程与 runner 侧**都导入它**，所以它不得
导入任何会泄漏真值的东西」，而真值类型就定义在它下面。策略进程必须导入
`types`（它要 `Response`），于是真值类型**顺带进了子进程的运行时命名空间**。

**一个类定义本身不是秘密**——`Stage1Truth` 是个空容器，知道它的形状不等于
知道答案；答案在 `config/` 里（那由 T10 的读路径黑名单管）。所以这次搬迁
**不是**在堵一个泄漏，而是在**把边界变成可断言的结构**：

* 搬之前，能断言的最多是「真值**值**没跨过边」——而那要靠审阅每一条通路。
* 搬之后，可以直接断言「子进程的 `sys.modules` 里没有任何定义真值的模块」
  （见 `tests/unit/test_isolation.py::TestImportGraph`）。这是一条**结构**断言，
  新加一条通路时它会自动覆盖到。

换句话说：**便宜，且把一句没法验的原则换成了一条能红的测试。** 这里不对
「导入即泄漏」做任何夸大——`config/` 才是真值的一方。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Stage1Truth:
    """Punching Bag 原样的真值 —— 没有「路由器」这个概念。

    与 `Stage2Truth` 拆成两套类型（设计方案 `:340`）：Stage 1 只能填
    `prefixes`，而 `n_routers` 嵌在 `prefixes` 的值里，**在唯一可填的字段
    内部无法填**。拆开之后不需要在运行时写 `if stage == 1` 分支。
    """

    prefixes: dict[str, dict[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Stage2Truth:
    """`RouterShim` 之上：合成的最后一跳让下面三个字段有意义（设计方案 `:353`）。

    `topology` / `seed_map` 是 D7 明令**不得单独喂给任何基线**的两个字段——
    它们在这里定义，不等于它们可以被交出去。
    """

    routers: frozenset[str] = frozenset()
    prefixes: dict[str, dict[str, object]] = field(default_factory=dict)
    topology: dict[str, tuple[str, ...]] = field(default_factory=dict)
    seed_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
