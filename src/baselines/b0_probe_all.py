"""`B0` —— 「全探一遍」：平凡基线与**退化警报**（T13 / D10）。

设计方案 `:698` 的原文：「**并跑一个平凡基线 B0「全探一遍」**：若 B0 也能拿到接近
最优的分数，说明任务退化（`E` 塌缩成 aliased/not 二分），**这是可观测的警报，
必须重新设计 config**。」

---

## 一、B0 到底做什么

**把预算平均分给每一个前缀，不看任何观测。** 没有反馈、没有自适应、没有记忆
——它是这套实验里唯一一个**完全无视回包内容**的策略。

这条定义有一处必须写清楚，否则 B0 与 B1 会被读成同一个东西：

| | 分配的是**什么** | 每前缀的探测数 |
|---|---|---|
| **B0** | **前缀**——每个前缀拿等量的一份 | **恰好** `budget / n`（±1，误差被摊平） |
| B1 | **地址空间**——在前缀集合里均匀随机取址 | **随机**，可能某个前缀一次没探到 |

**差别不在期望值，在覆盖的方差。** 全部 32 个前缀都是 `/64` 时，两者对同一个
前缀的**期望**探测数相同；但 B1 会给某个前缀 0 次、另一个 20 次，而 B0 的覆盖
是构造上确定的。这不是小事：`aliased_caught` 的定义是「**预算耗尽前**正确识别
的 aliased 前缀数」（`:491`），一个从没被探到的前缀根本不可能被识别，于是 B1
会在这一列上因**抽样噪声**掉分，而 B0 不会。两个基线在同一个端点上因为这种事
差开，会让「B4 比 B1 强」这类结论里混进一层与策略无关的噪声。

---

## 二、为什么是「警报」而不是「一条弱基线」

B0 的分数是**可以事先算出来的**，这才是它作为警报的价值：

* **`aliased_waste_ratio`**（花在 aliased 前缀上的预算占比）——B0 均分给每个
  前缀，于是它 **恰好等于 aliased 前缀在 config 里的占比**。本 config 是
  `8/32 = 25%`。
* **`aliased_caught`** —— **恒为 0，且是构造性的 0**（见第三节）。

而**理论上界**是另一个已知的数：一个知晓真值的策略可以完全绕开 aliased 前缀，
把 `aliased_waste_ratio` 压到 0、把 `aliased_caught` 拉满到 8。

**警报的判据因此是一句不等式**：若 `B0 的 aliased 占比` 离 0 太近（本 config
取「< 5%」为线，理由见下），那么「绕开 aliased」这件事**没有多少可省**，
`aliased_waste_ratio` 的可用跨度就不到容器侧容差（1%，`:739`）的几倍，
**任何策略都分不出来**——任务退化。25% 是这条线的 5 倍。

> **为什么线画在 5%**：单次运行的噪声地板是容器侧的 `回包计数差 ≤ 1%`（`:739`），
> 而它是这个比率端点的分母误差。信号要能被读出来，至少得有噪声的若干倍；
> 取 5 倍即 5%。这个数**是从容差推出来的，不是拍的**——但它仍然是本 config
> 的一个**选择**，改容差就要重算它。`tests/unit/test_stage1_config.py` 里那条
> 断言就是这条线的落点。

> **B0 测不到的那半个问题**（`:826`）：警报只看「任务是否塌缩成 aliased/not
> 二分」，**不看「基线的判定是否退化成常数」**。若某个档位的响应率高到让 B5
> 对任何前缀都判 aliased，B0 不会有任何反应。所以 `config/stage1_config.json`
> 的档位数值必须**另外**验一遍 B5 的可分辨性——那一条也在同一个测试文件里。

---

## 三、为什么 B0 **一次都不调 `report()`**

`report(prefix, aliased)` 是 `aliased_caught` 的**唯一**来源（`:488`，v6 加的
显式声明通道）。B0 没有判定环节——它不做任何推断，所以它**没有**可以声明的
东西。它在那一列上的 0 是**结构性 0**，不是「表现差」：

> 表注必须写明这一点（`:508`）。「这个端点对它不可测」与「它做得不好」如果
> 混着读，B0 会在一列它结构上无法参与的比较里被拿去和 B4 比，而那个比较
> 不成立。

代码上这件事是**没有代码路径**的，不是「记得别调」：这个模块不 import 任何
和声明有关的东西，`run()` 里也没有 `report` 这个字。

---

## 四、预算怎么分：为什么不用「前 `remainder` 个各多探一次」

`divmod(budget, n)` 会剩下一个余数。最省事的写法是把多出来的那几次给列表
**最前面**的那些前缀。**这条写法会让基线带上一个与实验无关的依赖**：多出来
的探测落在谁头上，取决于 config 文件里 `subnets` 的**书写顺序**。把 aliased
的八行从开头挪到中间，B0 测出来的 `aliased_waste_ratio` 就变了——而实验里
没有任何一个自变量是「config 的行序」。

所以这里用**累进除法**（Bresenham）把余数摊平：

```python
count_i = (budget * (i + 1)) // n - (budget * i) // n
```

每个前缀拿到 `⌊budget/n⌋` 或 `⌈budget/n⌉`，**总数恰好等于 `budget`**，而多出来
的那几次在整张列表上均匀分布，与任何前缀属性无关。副产品是本 config 上一个
恰好成立的好性质：aliased 前缀是列表的前 1/4，而均摊的分配对列表的任何一个
1/4 段给出的探测数都恰好是总数的 1/4 —— 于是 B0 的 `aliased_waste_ratio`
**精确等于 `8/32 = 0.25`**，没有 ±1/300 的余数抖动。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["DEGENERATE_WASTE_FLOOR", "plan_counts", "run"]


class _Surface(Protocol):
    """`run()` 需要的那一小片 `ProbeSurface`。

    只声明用到的两个成员，而不是 import `ProbeSurface`——接口在这里是
    **结构性的**：谁满足它谁就能当被测对象（单测里那句假 surface 就是一例）。
    这也是模块导入图干净的一种表达：这一层不需要知道 surface 的其余部分。
    """

    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def sample(self, prefix: str, n: int) -> list[object]: ...


#: 退化警报的线：B0 的 aliased 占比低于它，就说 `aliased_waste_ratio` 的可用
#: 跨度不够把策略分开。**理由见模块 docstring 第二节**——它是容器侧容差
#: （1%，`:739`）的 5 倍。改容差要重算这个数。
DEGENERATE_WASTE_FLOOR = 0.05


def plan_counts(prefixes: tuple[str, ...], budget: int) -> tuple[int, ...]:
    """把 `budget` 次探测**均摊**到 `prefixes` 上，返回每个前缀的探测数。

    用累进除法而不是「先各拿 `budget // n`，余数给前几个」——理由见模块
    docstring 第四节：后者的分配取决于列表顺序，而顺序是 config 的书写
    细节，不是实验的自变量。

    Raises:
        ValueError: `budget < len(prefixes)`。**这里必须炸**：预算连一遍都盖
            不住时，「全探一遍」这个定义本身不成立，而静默地只盖前 `budget`
            个前缀会让 B0 看起来跑完了——它测的是「配置有没有退化」，一个
            被静默削掉一半覆盖范围的 B0 恰恰会**掩盖**退化。
    """
    n = len(prefixes)
    if n == 0:
        raise ValueError("没有前缀就没有任务范围")
    if budget < n:
        raise ValueError(
            f"预算 {budget} 盖不住 {n} 个前缀——「全探一遍」不成立。"
            "这是 config/预算的错，不是策略的错：把预算提上去，或者把前缀数降下来。"
        )
    return tuple((budget * (i + 1)) // n - (budget * i) // n for i in range(n))


def run(surface: _Surface) -> None:
    """B0 的入口（`strategy_child` 按 `src.baselines.b0_probe_all:run` 加载）。

    契约是 `run(surface) -> None`（见 `strategy_child.DEFAULT_ENTRY_FUNCTION`）。

    **顺序是确定的**：按 `surface.prefixes` 给的顺序走。那是 runner 在握手时
    下发的任务范围，与 config 里的行序一致——但 B0 的分配不依赖这一点
    （见 `plan_counts`），它只是需要一个确定的遍历序来复现。

    Raises:
        ValueError: 握手没把预算带过来（`budget_left is None`）。**不猜一个
            默认值**：猜出来的预算要么花不完（浪费一次运行）要么被 runner 拒
            （`SurfaceError`），两种失败都比在这里直接说清楚难查。
    """
    budget = surface.budget_left
    if budget is None:
        raise ValueError(
            "surface.budget_left 是 None——握手没把初始预算带过来。"
            "见 ProbeSurface.__init__ 的 budget_left 参数与 strategy_child._hello。"
            "B0 不猜默认值：猜小了会花不完预算，猜大了会被 runner 拒绝，"
            "而两次都表现为「策略跑完了，结果不对」。"
        )

    counts = plan_counts(surface.prefixes, budget)

    # 逐前缀一次 `sample`，**不是**逐包一次 `probe`：`v6 (d)` 明令预算按
    # epoch 粒度预留而不是按包查询（`:397`），逐包往返会把并发池重新串行化。
    # B0 没有反馈要等，所以它可以一口气把计划发完。
    for prefix, count in zip(surface.prefixes, counts):
        surface.sample(prefix, count)
