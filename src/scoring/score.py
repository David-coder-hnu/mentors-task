"""`score()` —— 对着 `GroundTruth` 把一次运行折成**可数量**（T4 / D8）。

## 为什么这个模块可以读真值，而 `surface.py` 不可以

`score()` 住在 **runner 侧**。`oracle.py` 的 `ground_truth()` 写着「只有 runner
侧与 `score()` 该调它」——本模块就是那个 `score()`。策略进程不导入它；
`tests/unit/test_isolation.py::TestImportGraph` 盯的是「**策略侧**导入图里没有
`truth`」，而本模块**必须**有，否则它算的不是分。

## Stage 1 只算两个共主端点 + 一个次要诊断

- **共主端点**（设计方案 `:491`）：`aliased_waste_ratio`（花在 aliased 前缀上的
  预算占比）、`aliased_caught`（预算耗尽前**正确识别**的 aliased 前缀数）。
- **次要诊断**（`:482`）：`discovered_real_prefixes`——被证实「真有内容」的前缀
  数（去重）。
- **这里不算 `info_gain`**：v6 把它连同熵估计器整体移到 Stage 2 起点（`:1225`），
  因为它是次要量却和主端点挤在同一个 Stage 1 交付里，于是「跑出冒烟表」被一个
  不承重的估计器拖住。
- **这里不算 `true_routers`**：Stage 1 的 Punching Bag 没有路由器这个概念
  （`:624` 那张表的空列，以及 `truth.py` 里 Stage 1 / Stage 2 两套类型分开的理由）。

## 归属规则：**最长前缀匹配，与靶场的 trie 同一条**

一次探测落在哪个前缀上，靶场是用 `Node::getResponseRateForAddress` 沿 trie 下行、
**取最深的那个已插入节点**决定的（`IPv6-trie.hpp:285-311`）。评分器必须用同一条
规则——否则会出现「靶场按 `/48` 回的包，评分器按 `/64` 记的账」，而两边都自洽。

**对 `report()` 的声明用同一条规则（这是一个判断，另一个选项也说得通）。**
选项 A：声明必须**逐字命中** truth 的键，`2001:db8:1::/64` 声明在 `2001:db8::/48`
之下算「未命中」。选项 B（**本实现**）：声明的网络地址走同一次最长前缀匹配。
选 B 的理由有两条，第二条是承重的：

1. 一致性——「这个地址归哪个前缀管」在系统里只应该有一个答案，靶场已经给了它。
2. **选 A 会误伤 B5。** 6SEVEN 的在线判定要在 `/48 /64 /96 /112 /116 /120`
   六种长度上各发 3 次探测（`:644`）——一个照着它实现的 B5 很可能在 `/64` 上
   得出「这个 `/64` 全在回包」的结论并**照它测的那个长度**去声明。若 truth 的
   单位是 `/48`，选 A 会让 B5 这条**公平性协议里的最强基线**在一列上拿 0，
   而那是我们自己的记账口径造成的，不是它的能力问题。

   所以 B 还顺带被记了一笔：`reports_refined` 数出「有多少条声明不是逐字命中、
   而是被归并到某个 truth 前缀上」。**这个数留在这里是为了让口径可见**——
   若它很大，说明策略的粒度与 config 的粒度系统性错位，那本身是该知道的事。

## `aliased_caught` 单独用是可以被博弈的——所以这里多算了两列

按定义，`aliased_caught` 在「把每个前缀都声明成 aliased」这个平凡策略上取到
**最大值**，而两个共主端点里**另一个也不惩罚声明**（`aliased_waste_ratio` 只管
探测花在哪，不管你怎么报）。于是存在一条退化路径：不改进任何探测行为，只把
`report(p, True)` 打满，就在 co-primary 的一列上拿到满分。当前冒烟表里的五行
没有一条会走这条路（B0/B1 不声明，B4/M1/M2 与 B5 都按实际判定声明），所以它
**不是今天的数据缺陷，是端点定义的缺口**——而 A2 是会被交到 `report` 工具手里的
LLM。修它属于 T14（端点定义的归属地）；本模块先把 `aliased_false_positives` 与
`aliased_misses` 算出来，让这个缺口**有数可看**，而不是等它在表里以一个漂亮的
`aliased_caught` 出现。

## 重复声明的语义：**后者胜**，并且把翻供数出来

`report()` 可以被同一个前缀调多次（`SurfaceRunner.reports` 是一个按序的 list，
不是 dict）。「预算耗尽前**正确识别**」读起来是**运行结束时的最终判断**，所以取
该前缀最后一次声明。同一条前缀被先报 `True` 后报 `False`（或反过来）时计一次
`report_flip_flops`——**不抛异常，但也不静默**：翻供可能是「先猜后修正」，
也可能是在试探评分器，两者都该留痕。

> 「后者胜」与靶场读 config 时重复前缀的行为一致（`IPv6-trie.hpp:215-221`），
> 但**理由不同**：那边是实现细节，这边是端点定义的读法。

## 预算口径：**除数是「已计费探测数」，而 `send_failed` 也算**

`BudgetLedger.charge()` 是**先扣后跑**（`runner.py:79`），所以一次发包失败的探测
**已经花掉了预算**。把它从分母里剔掉，等于让「探测根本没发出去」比「发出去了没
回来」更便宜——而 `aliased_waste_ratio` 是个比率端点，**分母一动结论就动**
（`:736` 那条关于容差的推理是同一个道理）。所以：

- 分母 = 全部 `Response` 的条数（= 已计费探测数），含 `send_failed`。
- 分子 = 归属到 aliased 前缀的条数，**`send_failed` 也计入**（钱确实花在那个前缀上）。
- `send_failed` 单独记一笔：它让整轮**无效**（`RangeLedger.reconcile()` 会抛
  `RoundInvalid`），所以这一列非零时读的应该是「重跑」，不是「这个数偏低」。

## 0 次探测不是「占比 0」——而且它让**整次评分**拒答

`aliased_waste_ratio` 在 0 次探测上是 `0/0`，不是 0。写成 0 会让一次根本没跑起来
的运行在表里长得像一次完美的运行——本仓库反复点名的那一类失败：**静默、看起来
跑通了**。

**拒答的范围是一个判断：整次评分拒答，而不是只把 ratio 置 `None`。** 两个选项
都说得通，选中这个的理由：

- `None` 会一路传进配对检验，变成一个 NaN——**从「算不出来」退化成「算出来是
  NaN」**，而后者在下游的表现是「这个 seed 的结果消失」，不是报错。
- 一次零探测的运行意味着策略**没有参与实验**（预算是固定的，耗尽才是停止条件），
  所以它不是「一个有缺失字段的样本」，是「不该进表的东西」。

**代价要说清**：`aliased_caught` 在零探测的运行上是**良定义**的（它只读声明），
所以这条规则顺手也把它一起拒了。这不是疏忽，是取舍——但它意味着**「把每个前缀
都声明一遍、一次不探」这个策略连分都拿不到**。见下面那条缺口的说明：真正会打满
`aliased_caught` 的退化策略是有理由探测的（`aliased_waste_ratio` 是个**比率**，
少探非 aliased 的前缀只会让它变差），所以这个取舍不掩护那条路径。

> **「这一轮有效吗」不归本模块判。** 那是 `RangeLedger.reconcile()` 的事
> （T2）：它管丢包与对账，`score()` 只管把**已经收到的**东西折成数。这里拒答
> 的唯一理由是「这个数算不出来」，不是「这次实验作废」。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass

from ..adapter.truth import Stage1Truth, Stage2Truth
from ..adapter.types import Response

#: truth 里承载判定的那个键。T3 的 `stage1_truth_from_config()` 写的就是它
#: （`ground_truth.py`），这里读同一个名字。
_ALIASED_KEY = "aliased"


class ScoringError(ValueError):
    """**输入**不合规，算出来的数会是错的——所以在这里炸，不往下传。

    与 `ground_truth.ConfigError` 是同一类东西的两侧：那边管「config 本身有问题」，
    这边管「真值或运行记录喂进来的形状不对」。分开命名是因为两者的排查方向不同。
    """


# ── 输入 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次策略运行的**完整计分输入**——`score()` 的两个参数之一。

    刻意做成一个**纯数据结构**而不是去读 `SurfaceServer`：这样 `score()` 可以在
    没有靶场、没有 Docker、没有 socket 的情况下单测（`score()` 的 Verify 就是靠
    这个）。把 `SurfaceServer` 接上来产出一条 `RunRecord` 是 harness 的事，不是
    评分的事。

    Args:
        probes: 按**发生顺序**的响应。顺序有意义——`discovered_real_prefixes` 的
            「此前未确认」将来要读时序，而现在保留它是免费的。
        reports: `ProbeSurface.report()` 的调用，按序。`aliased_caught` 的**唯一**
            来源（`:488`）：评分器**不得**从策略的行为里推断它判了哪些前缀。
    """

    probes: tuple[Response, ...] = ()
    reports: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        # 收 `list` 但不留 `list`：frozen dataclass 里存一个可变对象，
        # 等于留了一条「构造完还能改输入」的路。
        object.__setattr__(self, "probes", tuple(self.probes))
        object.__setattr__(self, "reports", tuple(tuple(r) for r in self.reports))
        for i, probe in enumerate(self.probes):
            if not isinstance(probe, Response):
                raise ScoringError(f"probes[{i}] 是 {type(probe).__name__}，期望 Response")
        for i, report in enumerate(self.reports):
            if len(report) != 2:
                raise ScoringError(f"reports[{i}] 有 {len(report)} 个元素，期望 (prefix, aliased)")
            prefix, aliased = report
            if not isinstance(prefix, str):
                raise ScoringError(f"reports[{i}] 的前缀是 {type(prefix).__name__}，期望 str")
            if not isinstance(aliased, bool):
                # **bool 子类陷阱在这里更值得防**：`0`/`1` 会被当成正常值用下去，
                # 而它们在这个端点上的语义是没定义的。
                raise ScoringError(f"reports[{i}] 的 aliased 是 {aliased!r}，期望 bool")


# ── 输出 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Score:
    """一次运行的**可数量**折分结果。

    前两个是**共主端点**（判胜负用它们，判决规则见 `:526-530`）；`discovered_real_prefixes`
    是次要诊断（`:482`）；其余是**诊断列**——它们不参与判定，存在的理由是让上面的数
    可以被读懂、被质疑。`Score.__post_init__` 把几条恒等式钉死，所以一个算错的
    `Score` **构造不出来**，而不是等到被人读表时才发现。

    Attributes:
        aliased_waste_ratio: 共主端点。花在 aliased 前缀上的预算占比，`[0, 1]`。
        aliased_caught: 共主端点。被**正确**声明为 aliased 的 truth 前缀数（去重）。
        discovered_real_prefixes: 次要诊断。收到过回包、且 truth 说**非** aliased
            的前缀数（去重，`:482`）。
        aliased_false_positives: 诊断。声明为 `True` 但 truth 说不是 aliased 的前缀数。
            **与 `aliased_misses` 一起，是 `aliased_caught` 那个缺口的可见化**（见模块 docstring）。
        aliased_misses: 诊断。truth 说 aliased、而最终声明不是 `True` 的前缀数。
        probes_charged: 分母。已计费的探测数 = `len(RunRecord.probes)`。
        probes_unattributed: 诊断。落不进**任何** truth 前缀的探测数。它们花掉了预算
            但没花在 aliased 上，所以进分母、不进分子。
        send_failed: 诊断。发包失败的条数。**非零意味着整轮无效**，该重跑。
        reports_off_target: 诊断。归不到任何 truth 前缀的声明条数。
        reports_refined: 诊断。不是逐字命中、而是被最长前缀匹配归并的声明条数。
        report_flip_flops: 诊断。同一前缀被前后矛盾地声明的次数。
        aliased_prefixes_total: `aliased_caught` 的分母——truth 里一共几个 aliased
            前缀。**没有它，`aliased_caught = 3` 读不出是「3 个中的 3 个」还是
            「40 个中的 3 个」**，而这两个读法对同一个数字的含义完全相反。
        configured_prefixes_total: truth 里一共几个前缀（含沉默的那些）。
    """

    aliased_waste_ratio: float
    aliased_caught: int
    discovered_real_prefixes: int
    aliased_false_positives: int
    aliased_misses: int
    probes_charged: int
    probes_unattributed: int
    send_failed: int
    reports_off_target: int
    reports_refined: int
    report_flip_flops: int
    aliased_prefixes_total: int
    configured_prefixes_total: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.aliased_waste_ratio <= 1.0:
            raise ScoringError(f"aliased_waste_ratio={self.aliased_waste_ratio} 不在 [0, 1]")

        # **恒等式，不是合理性检查。** aliased 前缀被「最终声明为 True」与
        # 「没有」两分，所以这两块必须恰好铺满整个 aliased 集合。任何一边漏掉
        # 一条分支（比如忘了把 off-target 排除在外、或者去重去错了），这条立刻红。
        # 这是本模块唯一一条能同时钉住两个端点列的对账。
        if self.aliased_caught + self.aliased_misses != self.aliased_prefixes_total:
            raise ScoringError(
                f"aliased_caught({self.aliased_caught}) + aliased_misses({self.aliased_misses}) "
                f"!= aliased_prefixes_total({self.aliased_prefixes_total})——"
                "aliased 前缀没有被「抓住 / 漏掉」两块恰好铺满"
            )
        if not 0 <= self.aliased_false_positives:
            raise ScoringError("aliased_false_positives 不能为负")
        if not 0 <= self.probes_unattributed <= self.probes_charged:
            raise ScoringError(
                f"probes_unattributed={self.probes_unattributed} 不在 [0, {self.probes_charged}]"
            )
        if not 0 <= self.send_failed <= self.probes_charged:
            raise ScoringError(f"send_failed={self.send_failed} 不在 [0, {self.probes_charged}]")
        if self.probes_charged < 1:
            raise ScoringError("probes_charged 至少为 1——0 次探测的运行不该被折分")


# ── 真值索引 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _TruthIndex:
    """把 `GroundTruth.prefixes` 预索引成「最长前缀匹配」可用的形状。

    `by_length` 按 **prefixlen 降序**排列，所以「第一个命中的就是最长的那个」
    ——这正是靶场 trie 下行的语义，不需要再比一次。
    """

    by_length: tuple[tuple[ipaddress.IPv6Network, str, bool], ...]
    by_key: dict[str, bool]


def _index_truth(truth: Stage1Truth | Stage2Truth) -> _TruthIndex:
    entries: list[tuple[ipaddress.IPv6Network, str, bool]] = []
    by_key: dict[str, bool] = {}
    seen: dict[ipaddress.IPv6Network, str] = {}

    for raw, fields in truth.prefixes.items():
        if not isinstance(fields, dict):
            raise ScoringError(f"真值前缀 {raw!r} 的值是 {type(fields).__name__}，期望 dict")
        if _ALIASED_KEY not in fields:
            # 静默按「不是 aliased」处理，会让一个真值导出 bug 表现为
            # 「所有前缀都不是 aliased」——一个看起来很正常的表。
            raise ScoringError(f"真值前缀 {raw!r} 缺少 {_ALIASED_KEY!r} 字段")
        aliased = fields[_ALIASED_KEY]
        if not isinstance(aliased, bool):
            raise ScoringError(f"真值前缀 {raw!r} 的 {_ALIASED_KEY}={aliased!r}，期望 bool")

        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            raise ScoringError(f"真值前缀 {raw!r} 解析失败：{exc}") from exc

        # 两个键指向同一个网络时，「哪个算数」没有定义。T3 的 `read_subnets()`
        # 按规范化后的字符串做键，所以它的产物不会有这种形状；手写的真值可能有。
        # 与靶场读 config 时「重复前缀后者胜」是同一类静默歧义——这里选择炸。
        if net in seen:
            raise ScoringError(f"真值里 {seen[net]!r} 与 {raw!r} 是同一个网络 {net}，判定未定义")
        seen[net] = raw

        entries.append((net, raw, aliased))
        by_key[raw] = aliased

    entries.sort(key=lambda item: item[0].prefixlen, reverse=True)
    return _TruthIndex(by_length=tuple(entries), by_key=by_key)


def _match_address(addr: str, index: _TruthIndex) -> tuple[str | None, bool]:
    """地址 → (所属 truth 前缀, 该前缀是否 aliased)。落不进任何前缀时 `(None, False)`。"""
    try:
        ip = ipaddress.ip_address(addr.split("/")[0])
    except ValueError as exc:
        # **不降级成「未归属」。** 一个畸形地址是上游 bug（`ProbeSurface` 早就
        # 校验过），把它记进 `probes_unattributed` 会让它长得像「探到了任务范围外」。
        raise ScoringError(f"探测目标 {addr!r} 不是合法地址：{exc}") from exc
    for net, key, aliased in index.by_length:
        if ip in net:
            return key, aliased
    return None, False


def _match_report(prefix: str, index: _TruthIndex) -> tuple[str | None, bool, bool]:
    """声明的前缀 → (truth 前缀, 是否 aliased, 是否被归并)。

    先试**逐字命中**，再退回「把声明的网络地址走一次最长前缀匹配」。第二条是
    本模块的一个判断，理由见模块 docstring（选错会误伤 B5）。
    """
    if prefix in index.by_key:
        return prefix, index.by_key[prefix], False
    try:
        net = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        # 策略报了一个连解析都失败的串。这不是真值 bug，是策略行为——记一条
        # `reports_off_target` 就够了，不该让整次评分炸掉。
        return None, False, False
    key, aliased = _match_address(str(net.network_address), index)
    return key, aliased, key is not None


# ── 折分 ────────────────────────────────────────────────────────────────────


def score(run: RunRecord, truth: Stage1Truth | Stage2Truth) -> Score:
    """把一次运行对着真值折成 `Score`。

    Args:
        run: 这次运行的探测与声明（见 `RunRecord`）。**除数取自它**，不取自任何
            策略可写的账本——权威预算账本在 runner 侧（`runner.py:60`）。
        truth: `Oracle.ground_truth()` 的返回值。Stage 1 用 `Stage1Truth`；
            `Stage2Truth` 也接受（它的 `prefixes` 值同样带 `aliased`），
            但 Stage 2 的主端点 `true_routers` **不在这里算**。

    Raises:
        ScoringError: 空运行、真值缺字段、地址畸形，或真值里两个键撞在同一个网络。
    """
    index = _index_truth(truth)
    aliased_total = sum(1 for _, _, aliased in index.by_length if aliased)

    charged = len(run.probes)
    if charged == 0:
        raise ScoringError(
            "这次运行一次探测都没有：0/0 的比值不是 0，是「没测」。"
            "把它算成 0 会让一次没跑起来的运行看起来完美。"
        )

    # ── 探测侧 ──
    wasted = 0
    unattributed = 0
    send_failed = 0
    #: 收到过回包、且 truth 说非 aliased 的前缀 —— `discovered_real_prefixes`。
    #: 用集合而不是计数，是因为端点定义是「前缀数（去重）」（`:482`）。
    responded: set[str] = set()

    for probe in run.probes:
        if probe.status == "send_failed":
            send_failed += 1
        key, aliased = _match_address(probe.dst, index)
        if key is None:
            unattributed += 1
            continue
        if aliased:
            # **`send_failed` 也计进来**：预算在发包之前就扣了（`runner.py:79`），
            # 钱确实花在这个 aliased 前缀上。
            wasted += 1
        elif probe.status == "received":
            responded.add(key)

    # ── 声明侧 ──
    final: dict[str, bool] = {}
    off_target = 0
    refined = 0
    flips = 0

    for raw, aliased in run.reports:
        key, _truth_aliased, is_refined = _match_report(raw, index)
        if key is None:
            off_target += 1
            continue
        if is_refined:
            refined += 1
        if key in final and final[key] != aliased:
            flips += 1  # 前后矛盾：留痕，不抛异常（见模块 docstring）
        final[key] = aliased  # 后者胜

    caught = 0
    false_positives = 0
    for key, claimed in final.items():
        if not claimed:
            continue
        if index.by_key[key]:
            caught += 1
        else:
            false_positives += 1
    misses = aliased_total - caught

    return Score(
        aliased_waste_ratio=wasted / charged,
        aliased_caught=caught,
        discovered_real_prefixes=len(responded),
        aliased_false_positives=false_positives,
        aliased_misses=misses,
        probes_charged=charged,
        probes_unattributed=unattributed,
        send_failed=send_failed,
        reports_off_target=off_target,
        reports_refined=refined,
        report_flip_flops=flips,
        aliased_prefixes_total=aliased_total,
        configured_prefixes_total=len(index.by_length),
    )


__all__ = ["RunRecord", "Score", "ScoringError", "score"]
