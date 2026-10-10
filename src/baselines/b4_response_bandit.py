"""`B4` —— 响应率自适应 bandit：冒烟表的**记忆族无记忆控制组**（T6a / `:848`）。

冒烟表的五行里，`M1`/`M2` 是「有记忆」的那一对，`B4` 是它们的**对照**：
它同样**在线自适应**（按观测到的响应率改预算流向），但**不跨 run 记忆**——
每一个 `(策略, seed)` 都从零开始。`M2 vs B4` 是这张表的主比较（`:638`），
所以 B4 的定义必须写死到「差一定出在记忆上」为止。

---

## 一、算法：UCB1，教科书那一版

对每个前缀维护两个计数：`pulls_i`（探了几次）、`rewards_i`（回了几次）。
选择的臂是

```
score_i = rewards_i / pulls_i + sqrt(2 · ln(N) / pulls_i)      N = 总探测数
```

没被探过的臂（`pulls_i == 0`）记 `+inf`，**先探完再谈比较**——这是 UCB1 的定义
要求的（否则第 0 次除零），也是这里唯一一处「特殊值」。

`sqrt(2)` 是 UCB1 的**教科书常数**（Auer et al. 2002 的 `α = 2`），不是本 config
上调出来的：它是「无免费午餐」意义上唯一不需要知道奖励尺度就能用的值，
而**任何在评测拓扑上调出来的常数都违反 B5 的公平性协议（`:643`）的精神**——
B4 与 B5 在同一张表里，标准必须一样。

---

## 二、「一 epoch 一个决策」——为什么 B4 也得守这个节奏

`v6 (d)` 要求预算**按 epoch 粒度预留，不按包查询**（`:397`），而 `C11` 的
on-policy 记忆契约要求**第 t+1 个 epoch 依赖第 t 个 epoch 的已结算观测**（`:695`）。
两条合起来的意思是：**决策发生在 epoch 边界上，一次决策花掉一个 epoch 的预算。**

B4 不是 LLM，所以它不存在「一次 API 调用多少钱」的问题——但它仍然必须守这个
节奏，否则 `M2 vs B4` 这个主比较里会混进一个与记忆无关的自变量：**再决策的频率**。
M1/M2 每 epoch 决策一次（15 次），而一个逐包更新的 B4 会决策 300 次。那个差
不是「记忆有无」，是「反应快慢」。

所以：**轮数 `rounds = budget // EPOCH_SIZE`，每轮的大小由累进除法摊平。**

```python
size_t = (budget * (t + 1)) // rounds - (budget * t) // rounds
```

求和是望远镜式的，**恰好等于 `budget`**，而且余数摊在前几轮而不是全压在最后一轮。
这与 `b0_probe_all.plan_counts` 是同一个公式、同一条理由（B0 docstring 第四节：
余数的落点不得取决于列表顺序）。**两处刻意不互相 import**：`plan_counts` 分配的是
「每个前缀各多少」，这里分配的是「每一轮各多少」，两者的定义域不同，而
`b0_probe_all` 的归属是 T13 不是 T6a——一条 import 会把两个任务的交付期绑在一起。

`EPOCH_SIZE = 20` 是**从 config 抄来的第二个副本**（`config/stage1_config.json`
的 `epoch_size`）。这不是疏忽：`epoch_size` **今天没有任何通道进策略进程**
（握手只带 `prefixes` / `seed` / 预算视图；`test_stage1_config.py` 是唯一读它的
地方）。所以只能抄一份，并用一条测试把两份钉在一起——
`tests/unit/test_b4.py::test_the_epoch_size_matches_the_config` 直接读 config 比对，
改 config 不改这里就变红。

---

## 三、轮内怎么把 `size_t` 分到前缀上：**虚拟拉杆（virtual pulls）**

教科书 UCB1 是**逐次**的：选一个臂、看结果、更新。而这里一轮要一口气决定
`size_t` 次探测。直接把 UCB1 跑 `size_t` 次、每次都从**冻结的**统计里取 argmax
会让同一轮 20 个包全压在一个前缀上——argmax 不变，选择就不变。

所以这里做**虚拟拉杆**：规划第 j 次选择时，把「已经规划出去的那几次」当成已经
发生了——**假设该前缀继续以它已观测到的响应率回包**。

```
total_rewards[pick] += mean_pick      # mean 因此不变，只有探索奖励在缩小
pulls[pick] += 1
```

这条假设就是 trap 故事的原文：**「它一直在回，所以它应该继续会回」**——
B4 正是那个会持续把预算倒进 aliased 前缀的策略。虚拟拉杆让探索奖励
`sqrt(2·ln N / n_i)` 随规划收缩，于是高响应率的前缀拿走大头、没探过的前缀
仍能插进来。

**为什么假设的是「维持现有均值」而不是「奖励为 0」**：后者会让一个高响应率的
前缀在被规划 20 次之后均值趋近 0，于是规划自己把自己劝退——那既不是 UCB1，
也演不出 trap 曲线。前者是唯一与「观测到的就是最好的预测」这条朴素信念一致的
填法，而那条信念**写在一处**（`_virtual_pull`），可以单独测。

---

## 四、平局用 `surface.rng()`，而且**只在平局时用**

无观测时（全部 `pulls_i == 0`，或全部统计相同时）UCB1 的分数会精确相等，
而「相等时取下标最小的那个」会给列表前部一个**系统性的**优势。config 里 aliased
恰好排在最前面（`2001:db8:5:0::/64` 起 8 个），所以一条「取最小下标」的平局规则
会**白送 B4 一部分识别率**——而那部分来自 config 的行序，不是来自它的能力。

平局时在候选集合上均匀抽一个。用 `surface.rng()`，且**只调一次、把返回的
`Random` 接住**（`surface.rng()` 每次返回一个新的 `Random`；在循环里反复调它
等于每次都从序列第 0 个数重新开始，见 `ProbeSurface.rng` 的第二条规矩）。

种子进子进程的通道是 **T6a 定的握手**（协议 v2），不是环境变量。B4 是这条通道
的第二个用户（B1 是第一个），也是「同 seed 重跑必须逐包相同」在冒烟表里的落点。

---

## 五、`report()` 的规则：**参数自由**，而且只在最后报一次

`aliased_caught` 只能来自显式声明（`:488`），所以 B4 必须声明。规则是：

> **一个前缀只要「探过至少 2 次、且一次都没漏」，就声明它是 aliased。**

两个部分各有理由，而且**两条都是为了让规则里没有可调的阈值**：

* **「一次都没漏」** —— 真前缀的响应率严格小于 1（config 里最高的一档是 0.3），
  所以它**迟早会漏**。一次不漏地回了很多次，是 aliased 的签名本身。
* **「至少 2 次」** —— 只探过一次就下结论，等于把「这一次恰好回了」当签名；
  而 0.3 那一档有 30% 的概率恰好回。2 是让这条规则**非平凡**的最小数，
  不是调出来的。

**假阳性的算术要摆出来，不能藏着。** 一个真实响应率为 `r` 的前缀，拿到 `k` 次
探测全回的概率是 `r^k`。冒烟表里非 aliased 的前缀拿不到几次探测（UCB1 很快把
预算挪向 aliased），取 `k = 2`：`0.3² = 9%`、`0.1² = 1%`、`0.02² = 0.04%`。
**所以一整趟里出现 1 个左右假阳性是正常量级**，那不是 B4 写坏了。这个数由
`Score.aliased_false_positives` 逐趟报出来（`score.py` 模块 docstring 末段说的
正是这一列存在的理由），也是 T14 要修的那个端点缺口的一部分。

**只在最后报一次。** 声明是**最终判断**（`score.py`：「预算耗尽前正确识别」读作
运行结束时的最终判断，同前缀多次声明取后者），所以每一轮都报一次不会改变任何
得分，只会往 `report_flip_flops` 里灌噪声——而那一列的用途是标记**不确定**的
策略，不是标记**勤奋**的策略。

**探过 0 次的前缀不报。** 没有证据就没有判断；把它报成 `False` 会让
`aliased_misses` 少算一个「没识别出来」的前缀，而那正是这一列要说的事。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Protocol

__all__ = [
    "EPOCH_SIZE",
    "REPEAT_FLOOR",
    "UCB1_C",
    "Answered",
    "declarations",
    "plan_round",
    "round_sizes",
    "run",
]

#: 一个 epoch 的探测数。**这是 `config/stage1_config.json` 里 `epoch_size` 的
#: 第二个副本**——`epoch_size` 今天没有任何通道进策略进程（第二节）。
#: `tests/unit/test_b4.py::test_the_epoch_size_matches_the_config` 把两份钉在一起。
EPOCH_SIZE = 20

#: UCB1 的探索常数，教科书值 `sqrt(2)`（第一节）。
UCB1_C = math.sqrt(2.0)

#: 下判定所需的最少探测数（第五节）。**不是调出来的阈值**，是这条规则非平凡的
#: 最小取值：1 次就下结论等于把「这一次恰好回了」当签名。
REPEAT_FLOOR = 2


class Answered(Protocol):
    """`sample()` 返回值里 `run()` 用到的那一个字段。

    **只声明 `status`**：奖励是「回没回包」，而 `Response` 的其余七个字段
    （`src` / `icmp_code` / `rtt_ms` …）B4 一个都不看。这条声明是接口的**全部**——
    多声明一个字段，就等于给「B4 凭什么这么判」留一个读代码才能回答的问题。
    """

    status: str


class _Surface(Protocol):
    """`run()` 需要的那一小片 `ProbeSurface`。接口是**结构性**的（同 B0/B1）。"""

    prefixes: tuple[str, ...]

    @property
    def budget_left(self) -> int | None: ...

    def sample(self, prefix: str, n: int) -> list[Answered]: ...

    def report(self, prefix: str, aliased: bool) -> None: ...

    def rng(self) -> random.Random: ...


def _virtual_pull(pulls: list[int], rewards: list[float], index: int) -> None:
    """把「再探一次这个前缀」记进规划侧，**假设它维持现有响应率**（第三节）。

    均值因此不变，只有探索奖励跟着 `pulls` 缩小。抽出来单独一个函数，是因为
    这条假设是 B4 里最容易被改错的一行：改成「假设奖励为 0」会让规划自己把
    高响应率的前缀劝退，而症状是**一条画不出来的 trap 曲线**——没有任何一处
    会红，只是结果不对。
    """
    mean = 0.0 if pulls[index] == 0 else rewards[index] / pulls[index]
    rewards[index] += mean
    pulls[index] += 1


def _scores(pulls: Sequence[int], rewards: Sequence[float], total: int) -> list[float]:
    """每个前缀的 UCB1 分数。没探过的记 `+inf`（第一节）。"""
    out: list[float] = []
    for i, n in enumerate(pulls):
        if n == 0:
            out.append(math.inf)
            continue
        # `total ≥ n ≥ 1` 保证 `log` 的参数 ≥ 1、分母 ≥ 1，两条都不需要特判。
        out.append(rewards[i] / n + UCB1_C * math.sqrt(math.log(total) / n))
    return out


def plan_round(
    pulls: Sequence[int],
    rewards: Sequence[float],
    round_size: int,
    rng: random.Random,
) -> tuple[int, ...]:
    """规划一轮：`round_size` 次探测分给各前缀各几次。

    `pulls` / `rewards` 是**真实观测**（本轮的观测还没有），返回值是本轮的分配。
    函数**不改动入参**：它拷一份再做虚拟拉杆（第三节），所以「统计只在轮边界
    更新」这条契约在类型上就成立——调用方不接住返回值就什么也不会发生。

    Args:
        pulls: 每个前缀**已经**探过的次数。
        rewards: 每个前缀**已经**回过包的次数。**浮点**：虚拟拉杆往里加的是
            均值（可能是分数），而它是规划侧的量，不是真实计数。
        round_size: 本轮要花掉的探测数。
        rng: 平局用（第四节）。调用方接住一次传进来，见 `run()`。

    Raises:
        ValueError: `pulls` 与 `rewards` 长度不同、长度为 0，或 `round_size < 1`。
            **不静默补零**：长度不同意味着一处记账 bug，而它的现场会是「某个
            前缀的分数用了另一个前缀的统计」——一个更低的分数，看起来像噪声。
    """
    if len(pulls) != len(rewards):
        raise ValueError(f"pulls({len(pulls)}) 与 rewards({len(rewards)}) 长度不同")
    if not pulls:
        raise ValueError("没有前缀就没有臂")
    if round_size < 1:
        raise ValueError(f"一轮至少要 1 次探测，得到 {round_size}")

    planned_pulls = list(pulls)
    planned_rewards = [float(r) for r in rewards]
    total = sum(planned_pulls)
    plan = [0] * len(planned_pulls)

    for _ in range(round_size):
        scores = _scores(planned_pulls, planned_rewards, total)
        best = max(scores)
        ties = [i for i, score in enumerate(scores) if score == best]
        # 平局必须随机（第四节）：取最小下标会给列表前部系统性优势，而 config
        # 里 aliased 恰好排在最前面。
        if len(ties) == 1:
            pick = ties[0]
        else:
            pick = ties[int(rng.random() * len(ties))]
        plan[pick] += 1
        _virtual_pull(planned_pulls, planned_rewards, pick)
        total += 1

    return tuple(plan)


def round_sizes(budget: int, rounds: int) -> tuple[int, ...]:
    """把 `budget` 摊到 `rounds` 轮上，**总和恰好是 `budget`**（第二节）。

    与 `b0_probe_all.plan_counts` 同一个累进除法、同一条理由：余数不得全压在
    最后一轮（那会让最后一个 epoch 明显更长，而 epoch 长度是排空窗口的因子）。
    """
    if rounds < 1:
        raise ValueError(f"至少要一轮，得到 {rounds}")
    if budget < rounds:
        raise ValueError(f"预算 {budget} 盖不住 {rounds} 轮——每轮至少 1 次")
    return tuple(
        (budget * (t + 1)) // rounds - (budget * t) // rounds for t in range(rounds)
    )


def declarations(
    prefixes: Sequence[str],
    pulls: Sequence[int],
    rewards: Sequence[float],
) -> list[tuple[str, bool]]:
    """按第五节那条规则给出**最终**声明，按 `prefixes` 的顺序。

    **抽出来单独一个函数是为了让它可测。** 规则只有一行，但它是 `aliased_caught`
    的**唯一**来源（`:488`），而把它内联在 `run()` 末尾意味着唯一能测它的办法是
    跑一趟 300 探的会话——那时测的是 UCB1 的规划，不是这条规则，于是
    「一次不漏地回了很多次」与「只探一次恰好回了」这两条分支里，
    **后者根本走不到**。它是本模块唯一一处「阈值」，看不见就没法质疑。

    Args:
        prefixes: 全部臂，顺序与 `pulls` / `rewards` 对齐。
        pulls: 每个前缀**真实**探过的次数（不是规划侧的虚拟拉杆）。
        rewards: 每个前缀**真实**回过包的次数。

    Returns:
        `(前缀, 是否 aliased)` 的列表。`pulls == 0` 的前缀**不出现**——
        没有证据就没有判断（第五节）。

    Raises:
        ValueError: 三个序列长度不一致。静默按短的截断会让某个前缀的统计
            用了另一个前缀的计数，而那表现为一条**错的**声明，不是报错。
    """
    if len(prefixes) != len(pulls) or len(prefixes) != len(rewards):
        raise ValueError(
            f"prefixes({len(prefixes)}) / pulls({len(pulls)}) / rewards({len(rewards)}) "
            "长度不一致——统计错位会静默产出一条错的声明"
        )

    out: list[tuple[str, bool]] = []
    for index, prefix in enumerate(prefixes):
        if pulls[index] == 0:
            continue
        out.append((prefix, pulls[index] >= REPEAT_FLOOR and rewards[index] == pulls[index]))
    return out


def run(surface: _Surface) -> None:
    """B4 的入口（`strategy_child` 按 `src.baselines.b4_response_bandit:run` 加载）。

    九步：读预算 → 定轮数 → 逐轮（规划 → 执行 → 按回包计数）→ 最后声明一遍。

    Raises:
        ValueError: 握手没给预算、没有前缀、预算盖不住一个 epoch，或 `sample`
            回来的条数与要的不符。**每一条都必须炸**——它们都会让「花了多少、
            回了多少」这两个计数错位，而错位的现场是一个能折出分的、看起来
            正常的 `Score`。
    """
    budget = surface.budget_left
    if budget is None:
        raise ValueError(
            "surface.budget_left 是 None——握手没把初始预算带过来。"
            "见 ProbeSurface.__init__ 的 budget_left 参数与 strategy_child._hello。"
            "B4 不猜默认值：猜小了会花不完预算，猜大了会被 runner 拒绝，"
            "而两次都表现为「策略跑完了，结果不对」。"
        )

    prefixes = surface.prefixes
    if not prefixes:
        raise ValueError("没有前缀就没有臂")
    if budget < EPOCH_SIZE:
        # 静默跑 0 轮会得到一次「0 次探测」的运行，而 `score()` 对它是**拒答**
        # （`score.py` 末段）——那个失败会报在评分器里，离真正的原因隔着一层。
        raise ValueError(
            f"预算 {budget} 小于一个 epoch（{EPOCH_SIZE}）——这一趟连一次决策都"
            "做不了。这是 config/预算的错，不是策略的错。"
        )

    rounds = budget // EPOCH_SIZE
    sizes = round_sizes(budget, rounds)
    # **接住一次**（第四节）：`surface.rng()` 每次返回一个新的 `Random`，
    # 在循环里反复调它等于每次都从序列头开始。
    rng = surface.rng()

    pulls = [0] * len(prefixes)
    rewards = [0] * len(prefixes)

    for size in sizes:
        plan = plan_round(pulls, rewards, size, rng)
        # 按 `prefixes` 的顺序发，不按规划的产生顺序——同 B0/B1：让「发出去哪些
        # 请求」只取决于分配，不取决于另一个要复现的东西。
        for index, count in enumerate(plan):
            if count == 0:
                continue  # 0 次必须跳过，`sample` 在 n < 1 时抛（B1 第四节）
            responses = surface.sample(prefixes[index], count)
            if len(responses) != count:
                raise ValueError(
                    f"{prefixes[index]} 上要了 {count} 个响应，回来 {len(responses)} 个"
                    "——计数会错位，而错位的现场是一个能折出分的表"
                )
            pulls[index] += count
            rewards[index] += sum(1 for r in responses if r.status == "received")

    for prefix, aliased in declarations(prefixes, pulls, rewards):
        surface.report(prefix, aliased)
