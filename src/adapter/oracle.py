"""`Oracle` —— **唯一的真值出口**，只在 runner 进程里（D7 / 设计方案 `:383`）。

这个模块与 `surface.py` 是**同一条边两侧**的两个对象：

| | 住在哪 | 能做什么 |
|---|---|---|
| `ProbeSurface` | 策略进程 | 发包、要回包、声明判定。**没有任何真值** |
| `Oracle` | runner 进程 | 读真值、重置靶场。**策略拿不到** |

`v6-2` 把 `reset` 拆成两半的理由写在 `surface.reset()` 的 docstring 里：
**策略无权重置靶场**（`:322`），因为重置靶场等于改写实验条件。

> **这里的隔离靠的是进程边界，不是「别 import 我」。** 类型边界（`hasattr`
> 断言）在 v5 被显式删除：`A2 会写代码`，绕过一句 `hasattr` 是平凡的
> （`:389`）。所以本模块不设任何自我防卫——**防卫在 `runner` 与
> `strategy_child` 之间那条 stdio 上**，以及 `tests/unit/test_isolation.py`
> 里那条跑真子进程的断言。
"""

from __future__ import annotations

from collections.abc import Callable

from .truth import Stage1Truth, Stage2Truth

#: 真值的来源。Stage 1 是「读 config → 判 rate」，Stage 2 是 shim 的合成结果；
#: 两种情况都是**一次性导出**，所以这里是个无参可调用对象而不是逐前缀接口
#: ——逐前缀接口会让 T3 的实现有机会把「未配置」与「rate==0」混在一起，
#: 而那正是失败模式表 `:1160` 点名要区分的一对。
TruthSource = Callable[[], "Stage1Truth | Stage2Truth"]


class Oracle:
    """runner 侧的真值出口。

    Args:
        truth_source: 无参可调用对象，返回当前真值。**必填**——一个默认的
            「空真值」会让「忘了接上 T3 的导出」表现为「所有前缀都不是
            aliased」，于是评分给出一个看起来很正常的数。没有默认值，
            漏接就是 `TypeError`。
        reset_target: `(seed) -> None`，把靶场重置到该 seed 的状态。
            **必填**，理由同上：缺省成空操作会让 A/B 对比悄悄失去可重复性
            （设计方案 `:325` 说 `reset_target(seed)` 正是让对比严格可重复的
            那一环），而缺了它两次 run 之间靶场不会回到同一状态——症状是
            「结果飘」，不是报错。
    """

    def __init__(self, *, truth_source: TruthSource, reset_target: Callable[[int], None]) -> None:
        if not callable(truth_source):
            raise TypeError("truth_source 必须是可调用对象")
        if not callable(reset_target):
            raise TypeError("reset_target 必须是可调用对象")
        self._truth_source = truth_source
        self._reset_target = reset_target
        #: 重置过几次、每次的 seed —— 让「A/B 真的重置了吗」可事后核对。
        self.reset_log: list[int] = []

    def ground_truth(self) -> Stage1Truth | Stage2Truth:
        """读当前真值。**只有 runner 侧与 `score()` 该调它。**"""
        return self._truth_source()

    def reset_target(self, seed: int) -> None:
        """把**靶场**重置到 `seed` 的状态。策略侧没有通往这里的路。"""
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError(f"seed 必须是整数，得到 {seed!r}")
        self._reset_target(seed)
        self.reset_log.append(seed)
