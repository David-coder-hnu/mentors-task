"""`B1` —— 均匀随机：冒烟表的**下界**（T6a / 设计方案 `:848`）。

`B0` 把预算**均摊**给前缀（每个前缀拿等量的一份），B1 把预算**均匀随机地撒**
在地址空间上。两者对同一个前缀的**期望**探测数相同（32 个前缀都是 `/64`，等大），
差的是**覆盖的方差**——这正是 `b0_probe_all.py` 模块 docstring 第二节画的那张表。

---

## 一、B1 到底做什么

**抽 `budget` 次，每次在前缀集合里等概率选一个前缀。** 于是每个前缀拿到的探测数
是一个**多项分布**的实现：期望 `budget / n`，方差 `budget · (1/n) · (1 - 1/n)`。

**它不看任何观测、不声明任何判定。** 与 B0 一样，它是这条表里「没有反馈」的那一半；
区别在于 B0 的覆盖是构造上确定的，B1 的是随机的。作为**下界**，它回答的问题是：
「一个完全不知道自己在干什么、但至少把预算花在了任务范围内的策略，能拿到多少分？」

---

## 二、随机源：`surface.rng()`，而且**只用 `random()`**

种子由握手送进来（T6a / 协议 v2），策略侧唯一的取用口是 `ProbeSurface.rng()`。
三条规矩写在那个方法的 docstring 里，这里用的是前两条：

* **只调 `rng.random()`**，不用 `randrange` / `shuffle` / `sample`。Python 只保证
  `random()` 那条序列跨版本不变；用别的会让「同 seed 重跑逐包相同」在升一次
  Python 之后**悄悄失效**——而失效的现场是一张看起来正常、实际不可复现的表。
* **`rng()` 只调一次**，把返回的 `Random` 接住再反复用。`rng()` 每次返回一个
  **新的** `Random`，所以在循环里反复调它等于每次都从序列第 0 个数重新开始——
  一道「所有随机数都相同」的静默失败。

---

## 三、抽样：为什么是 `int(rng.random() * n)` 而不是 `rng.randrange(n)`

`rng.random()` 的取值域是 `[0.0, 1.0)`，所以 `int(r * n)` 落在 `[0, n-1]` 里，
**恰好**覆盖全部前缀且不会越界。这是「均匀」在这个规模上唯一需要的性质。

`randrange` 做的是同一件事，但它**不保证跨版本稳定**（第二节）。两者在这里的
差别不是性能也不是风格，是**可复现性写在官方保证的那一侧**。

**注意它与「均匀取地址」的等价性**：config 里 32 个前缀全是 `/64`，等大，
所以在「前缀集合上等概率取一个」与「在整个地址空间上等概率取一个地址」是
**同一个分布**。将来若 config 里混入不同长度的前缀，这个等价性就断了，
届时 B1 要改成按前缀大小加权——那是一个**已知的**、还没到的改动。

---

## 四、零次的前缀**跳过**，不是 `sample(prefix, 0)`

`ProbeSurface.sample()` 在 `n < 1` 时抛 `ValueError`（`surface.py:179`），
`SurfaceServer._dispatch` 同样（`runner.py:177`）。所以分配里出现 0 的前缀必须
**根本不发那条请求**——写成 `sample(prefix, 0)` 会被 runner 拒，表现是一场跑到
一半 `SurfaceError` 的会话。

**这条分支在这里是承重的，不是防御性编程**：B1 与 B0 的全部差别就是「有些前缀
拿到 0 次」，而这条分支正是那个差别的落点。把它当成不可能发生而省掉，B1 就退化
成了 B0（只是顺序不同）。

---

## 五、一个必须诚实说出的量化事实：**300 探 / 32 前缀下，B1 与 B0 几乎分不开**

`b0_probe_all.py` 的 docstring 写着「B1 会给某个前缀 0 次、另一个 20 次，而 B0 的
覆盖是构造上确定的」，并据此说两者会在 `aliased_caught` 上因为抽样噪声分开。
**那个论断在冒烟表的规模上不成立，这里是算给它听的：**

```
P(某个前缀一次都没被抽到) = (1 - 1/32)^300 = (31/32)^300 ≈ 7.3e-5
```

32 个前缀乘上去，一整趟里出现「某个前缀被完全跳过」的概率约 **0.23%**。
`aliased_waste_ratio` 同理：它的标准差约 `sqrt(p(1-p)/budget)` = `sqrt(0.25·0.75/300)`
≈ **2.5%**，而 B0 的那个数是构造上精确的 `0.25`。也就是说

> **在 300 探的规模上，B1 与 B0 在这两个共主端点上的差别小于一次运行的噪声。
> 冒烟表把它们同时放进去，读的**不是**「谁比谁强」，而是「随机撒点的下界在哪」。**

这不是 B1 的缺陷，是**冒烟规模**的性质。它有一个直接后果：**任何以「B1 的覆盖
方差比 B0 大」为理由的结论，都需要一个比 300 大的预算才立得住**——而那是 Stage 2
的完整表（1000 探）才谈得上的事。这条写在这里，是为了让下一个读那张表的
人不必自己重算一遍。

---

## 六、`B1` 一次都不调 `report()`

与 B0 同一条理由（`b0_probe_all.py` 第三节）：B1 不做任何判定，所以它**没有**可以
声明的东西。它在 `aliased_caught` 上的 0 是**结构性 0**，不是「表现差」。表注必须
写明这一点（`:508`）。

代码上同样是**没有代码路径**：本模块不 import 任何与声明有关的东西，`run()` 里
也没有 `report` 这个字。
"""

from __future__ import annotations

import random
from typing import Protocol

__all__ = ["plan_counts", "run"]


class _Surface(Protocol):
    """`run()` 需要的那一小片 `ProbeSurface`。

    只声明用到的成员，而不是 import `ProbeSurface`——接口在这里是**结构性**的，
    与 `b0_probe_all._Surface` 同一条理由。**注意这里没有 `report`**：B1 不声明
    任何判定，而「不给它那条路」比「记得别调」牢靠（第六节）。
    """

    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def sample(self, prefix: str, n: int) -> list[object]: ...

    def rng(self) -> random.Random: ...


def plan_counts(prefixes: tuple[str, ...], budget: int, rng: random.Random) -> tuple[int, ...]:
    """抽 `budget` 次，返回每个前缀被抽中的次数。

    **幂等地用 `rng`**：调用方把 `surface.rng()` 接住一次、传进来（第二节）。
    在本函数里再调一次 `surface.rng()` 会让「同一个 seed 给出同一条序列」这条
    性质只对**第一次**调用成立。

    Raises:
        ValueError: `prefixes` 为空，或 `budget < 1`。两者都是「这不是一次实验」，
            而静默返回一个全零的分配会让一次没跑起来的运行看起来跑完了。
    """
    n = len(prefixes)
    if n == 0:
        raise ValueError("没有前缀就没有任务范围")
    if budget < 1:
        raise ValueError(f"预算至少为 1，得到 {budget}")

    counts = [0] * n
    for _ in range(budget):
        # 见第三节：`random()` 是唯一被官方保证跨版本稳定的那一个。
        counts[int(rng.random() * n)] += 1
    return tuple(counts)


def run(surface: _Surface) -> None:
    """B1 的入口（`strategy_child` 按 `src.baselines.b1_uniform_random:run` 加载）。

    Args:
        surface: 只需要 `prefixes` / `budget_left` / `sample` / `rng`。

    Raises:
        ValueError: 握手没把预算带过来（`budget_left is None`）。**不猜默认值**，
            理由与 `b0_probe_all.run` 那段一字不差：猜小了花不完、猜大了被拒，
            两种都表现为「策略跑完了，结果不对」。
    """
    budget = surface.budget_left
    if budget is None:
        raise ValueError(
            "surface.budget_left 是 None——握手没把初始预算带过来。"
            "见 ProbeSurface.__init__ 的 budget_left 参数与 strategy_child._hello。"
            "B1 不猜默认值：猜小了会花不完预算，猜大了会被 runner 拒绝，"
            "而两次都表现为「策略跑完了，结果不对」。"
        )

    # **接住一次**，然后在整个分配里反复用（第二节）。
    rng = surface.rng()
    counts = plan_counts(surface.prefixes, budget, rng)

    # 按 `surface.prefixes` 的顺序逐前缀发一批（**不是**按抽中的先后），与 B0
    # 同一条理由：`v6 (d)` 明令预算按 epoch 粒度预留，逐包往返会把并发池串行化
    # （`:397`）。按前缀顺序而不是按抽样顺序，还让「哪些请求发出去」只取决于
    # 计数、不取决于抽签的先后——两个随机源里少一个要复现的东西。
    for prefix, count in zip(surface.prefixes, counts):
        if count == 0:
            continue  # 见第四节：0 次必须跳过，不能发出去
        surface.sample(prefix, count)
