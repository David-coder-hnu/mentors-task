"""决策体 —— 按**决策 epoch** 调一次 LLM，结果过缓存（设计方案 `:549`）。

**粒度是 epoch，不是包**（`:549`）：一个 epoch 里发若干探测、收一批回包，
然后做一次决策。于是总 API 调用量 = 策略数 × (1000 / epoch_size) × N。

## 这个模块在 Stage 1 的分量：**没有数字**

v6(a)（`:567`）把 A1/A2 挡在 Stage 1 门外，所以 T5 在 Stage 1 的交付物只有两样：
**决策缓存**与**状态规范化**。本模块存在的理由是让这两样**跑得通单测**——
它**不产生任何进入冒烟表的数字**，一个都不产生。Stage 2 原样复用的是缓存与
规范化，**不是决策质量**。把这句话写在这里，是因为「Stage 1 里有个能跑通的
LLM 决策体」很容易被后来者读成「Stage 1 有 LLM 结果」——它有代码，没有结果。

## `Completion` 是注入口，Stage 1 不接任何 API

`Completion = Callable[[str], str]`：**渲染后的 prompt 进，模型的原始文本出**。
Stage 1 的测试传一个假函数，于是 T5 可以在**没有网络、没有 API key、没有花费**
的情况下把缓存与规范化钉死。

> **`completion=None` 是「离线回放」模式，而且它是必须的。** 回放时任何一次
> 未命中都会抛 `DecisionError`，而不是偷偷联网再问一遍。理由与本仓库其它地方
> 同源：一次「看起来跑通了」的回放如果悄悄打了 API，它得到的就**不是**原来那次
> 实验的结果——它是一份新的、没人记录过的结果，而报告里写着它是复现。
> 这与 bug 4（内建 `hash()` 加盐，`:566`）是同一类失效：**静默地换掉了一个
> 你以为没变的东西。**

## 三个判断（schema 比 `protocol` 更严）

1. **多一个键就拒绝。** `protocol.response_from_wire` 就是这个口径（未知键一律
   拒绝，不是忽略），这里沿用。理由是忽略未知键等于**猜**模型想干什么，而猜错的
   方向是静默的。
2. **`reason` 必填且非空。** 这是 P1 那张表里「决策理由 | 可读」那一格的落点
   （`:509` 一带）：理由不是装饰，它**是**可读性的载体。一个可以省掉的理由，
   在第一次赶时间时就会被省掉。
3. **`aliased` 必须是 JSON 的 `true` / `false`，不是 `1` / `0`。** 这条单独写一条
   错误消息：`1` 在 Python 里是 `int` 且 `bool` 是它的子类，`isinstance(x, bool)`
   之外还有 `x in (0, 1)` 这种写法能"通过"，而 prompt 里已经明说了不要。**它必
   须炸**，因为把 `1` 收下来之后，`aliased_caught` 这个端点算的是布尔，静默地
   接受了 `1` 意味着某一天有人写成 `2` 也会被收。

## `Stage1State` 为什么是一堆计数器

它是**要进缓存键的状态**，所以它的每一个字段都必须**按构造与到达顺序无关**
（`:564`）。计数器（加法）、`frozenset`（集合）、`bool`（或）都满足；
`list`、墙钟、`dict` 的插入顺序都不满足。于是这里**没有一个字段是 `list`**——
不是洁癖，`canonical.py` 会直接把 `list` 拒掉，两者是同一件事的两端。

**墙钟被换成了 `epoch`**：`:564` 明写「时间类字段用逻辑序号而非墙钟」。`epoch`
就是那个逻辑序号，它进 key，也因此**同一个状态在两次运行里必须得到同一个 epoch**。
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

from ..adapter.types import Response
from . import prompts
from .cache import CacheKey, DecisionCache
from .primitive import ProgramStep, parse_steps

#: LLM 调用的注入口：**渲染后的 prompt 文本进，模型的原始输出文本出**。
#:
#: 做成一个函数类型而不是一个客户端类，是刻意的：于是「Stage 1 不接 API」这件事
#: 不需要靠 code review 保证——本模块**没有**任何可以联网的 import。
Completion = Callable[[str], str]

#: LLM 身份必须钉住的六项（设计方案 `:588`）。`config` 里缺任何一项就拒绝开工。
#:
#: 为什么是硬检查而不是"记得写"：`config` 整份进缓存键（`cache.py`），所以
#: 身份**已经在哈希里**了；这里拦的是另一半——**一份没有身份的 config，
#: 它的键是合法的、缓存是可加载的、结果看起来完全正常**，只是没人知道
#: 它对应哪个模型。这正是"静默"的形状。
LLM_IDENTITY_KEYS = (
    "model",
    "model_version",
    "temperature",
    "top_p",
    "max_tokens",
    "call_date",
)


class DecisionError(RuntimeError):
    """决策这一层出的错（缓存未命中且没有 completion、状态里出现未知前缀…）。"""


class MalformedDecision(DecisionError):
    """模型的输出不是一个合法动作。

    **带原文**（`self.raw`）：畸形输出要能被人看见长什么样，否则唯一的处置
    就是重跑一次然后祈祷——而复现问题需要的正是那串原文。
    """

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def require_llm_identity(config: Mapping[str, Any]) -> None:
    """检查 `config` 里 LLM 身份的六项齐全（`:588`）。

    Raises:
        DecisionError: 缺项。**列出缺了哪几项**——只说"config 不合法"会让人
            去猜，而猜的成本在"温度"这种字段上是不可见的（差 0.1 就不是同一次
            实验了，`:588` 要求它进 config 正是因为这一点）。
    """
    missing = [key for key in LLM_IDENTITY_KEYS if key not in config]
    if missing:
        raise DecisionError(
            f"config 缺少 LLM 身份字段 {missing}。设计方案 `:588` 要求模型 ID、版本、"
            "温度、top_p、max_tokens、调用日期六项全部写进 config 并随缓存一起发布；"
            "缺项时这份缓存不知道对应哪次调用，复现性边界（`:592`）无从谈起。"
        )


# ── 工具池 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Tool:
    """策略可以调用的一个原语。`description` **是建模的一部分**：它进 prompt、
    也进缓存键（`cache.CacheKey` 的 `tool_pool` 分量）。"""

    name: str
    description: str


class ToolPool:
    """一组工具，**按名字规范排序**。

    ## 池的内容不在这里

    A1 与 A2 的唯一差别就是**工具池**（A1 固定，A2 允许构造新原语，`:618`），
    而两者之差就是 P1 的全部内容。所以「池里有哪些工具」是**自变量**，
    归 T10（A2 的沙箱与允许清单）定；本模块只提供容器与**唯一的**排序规则。

    > **排序只在这里做一次。** `prompts.render()` 明确不再排一次：两处排序规则
    > 将来会漂，而漂的后果是「缓存键算的是 A 顺序、模型看到的是 B 顺序」——
    > 又是静默失效。
    """

    __slots__ = ("_tools",)

    def __init__(self, tools: Iterable[Tool]) -> None:
        by_name: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in by_name:
                raise ValueError(
                    f"工具名重复：{tool.name!r}。同名两条会随 `ToolPool` 的构造顺序"
                    "决定谁生效，而构造顺序是调用方的事——那正是'顺序有没有语义'"
                    "又一次没被显式决定。"
                )
            by_name[tool.name] = tool
        self._tools: tuple[Tool, ...] = tuple(by_name[name] for name in sorted(by_name))

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools)

    def __contains__(self, name: object) -> bool:
        return any(tool.name == name for tool in self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

    def as_pairs(self) -> tuple[tuple[str, str], ...]:
        """喂给 `prompts.render()` 的形状。**已排序。**"""
        return tuple((tool.name, tool.description) for tool in self._tools)

    def as_mapping(self) -> Mapping[str, str]:
        """喂给 `cache.CacheKey` 的形状。**整份"名字 → 描述"入键**，
        理由见 `cache.py`：同名但描述变了与多了一个工具对决策的影响同类。"""
        return MappingProxyType({tool.name: tool.description for tool in self._tools})

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ToolPool({', '.join(self.names) or '空'})"


# ── 动作 schema ───────────────────────────────────────────────────────────

ActionKind = Literal[
    "probe", "report", "final_report", "construct", "compose", "stop"
]

#: 每种动作**恰好**允许的键。多一个拒绝、少一个拒绝——**没有第三个选项**。
_ACTION_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "probe": frozenset({"action", "prefix", "count", "reason"}),
        "report": frozenset({"action", "prefix", "aliased", "reason"}),
        "final_report": frozenset({"action", "reports", "reason"}),
        "construct": frozenset(
            {"action", "name", "blueprint", "count", "reason"}
        ),
        "compose": frozenset({"action", "name", "steps", "count", "reason"}),
        "stop": frozenset({"action", "reason"}),
    }
)

CONSTRUCTION_BLUEPRINTS = frozenset({"balanced_probe"})


@dataclass(frozen=True, slots=True)
class Action:
    """一个通过校验的动作。`kind` 决定哪几个可选字段非 None。"""

    kind: ActionKind
    reason: str
    prefix: str | None = None
    count: int | None = None
    aliased: bool | None = None
    name: str | None = None
    blueprint: str | None = None
    reports: Mapping[str, bool] | None = None
    steps: tuple[ProgramStep, ...] | None = None


def parse_action(text: str, *, origin: str = "模型输出") -> Action:
    """把模型的原始文本解析成一个**通过 schema 校验**的动作。

    这是「schema 校验拒绝畸形输出」那条 Verify 的落点，也是设计上刻意放在
    **关键路径上**的一步：`DecisionCache` 只存原始文本（`cache.py`），于是
    每一次命中都要重新过这里。**缓存不能成为绕过校验的后门。**

    Args:
        text: 模型返回的原文（或缓存里存着的原文）。
        origin: 出错消息里指认它从哪来（"模型输出" / "缓存"）。命中路径上
            这两个来源的处置完全不同，所以错误消息必须能区分。

    Raises:
        MalformedDecision: 不是 JSON 对象、`action` 不认识、键集不匹配、
            `reason` 空、`count` 非正整数、`aliased` 不是布尔、前缀不是合法网络。
    """
    def bad(detail: str) -> MalformedDecision:
        return MalformedDecision(f"{origin}不是合法动作：{detail}\n原文：{text!r}", raw=text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise bad(f"不是合法 JSON（{exc.msg}）") from exc

    if not isinstance(payload, dict):
        raise bad(f"顶层必须是 JSON 对象，得到 {type(payload).__name__}")

    kind = payload.get("action")
    if kind not in _ACTION_FIELDS:
        raise bad(f"`action` 必须是 {sorted(_ACTION_FIELDS)} 之一，得到 {kind!r}")

    allowed = _ACTION_FIELDS[kind]
    extra = sorted(set(payload) - allowed)
    if extra:
        raise bad(
            f"`{kind}` 不允许多余的键 {extra}（允许的恰好是 {sorted(allowed)}）。"
            "**未知键一律拒绝而不是忽略**——忽略等于猜模型想干什么，而猜错的方向是静默的。"
        )
    missing = sorted(allowed - set(payload))
    if missing:
        raise bad(f"`{kind}` 缺少必需的键 {missing}")

    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise bad(f"`reason` 必须是非空字符串，得到 {reason!r}。P1 的「决策理由 | 可读」"
                  "那一格要求每一个决定都能被读出来为什么（设计方案 `:509` 一带）。")

    if kind == "stop":
        return Action(kind="stop", reason=reason)

    if kind == "construct":
        name = payload["name"]
        blueprint = payload["blueprint"]
        count = payload["count"]
        if (
            not isinstance(name, str)
            or not name
            or not name.replace("_", "").replace("-", "").isalnum()
            or not name[0].isalpha()
        ):
            raise bad(f"`name` 必须是字母开头的标识符，得到 {name!r}")
        if blueprint not in CONSTRUCTION_BLUEPRINTS:
            raise bad(
                f"`blueprint` 必须是 {sorted(CONSTRUCTION_BLUEPRINTS)} 之一，"
                f"得到 {blueprint!r}"
            )
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise bad(f"`count` 必须是正整数，得到 {count!r}")
        return Action(
            kind="construct",
            reason=reason,
            count=count,
            name=name,
            blueprint=blueprint,
        )

    if kind == "compose":
        name = payload["name"]
        count = payload["count"]
        if (
            not isinstance(name, str)
            or not name
            or not name.replace("_", "").replace("-", "").isalnum()
            or not name[0].isalpha()
        ):
            raise bad(f"`name` 必须是字母开头的标识符，得到 {name!r}")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise bad(f"`count` 必须是正整数，得到 {count!r}")
        try:
            steps = parse_steps(payload["steps"])
        except Exception as exc:
            raise bad(str(exc)) from exc
        return Action(
            kind="compose",
            reason=reason,
            count=count,
            name=name,
            steps=steps,
        )

    if kind == "final_report":
        raw_reports = payload["reports"]
        if not isinstance(raw_reports, dict) or not raw_reports:
            raise bad("`reports` 必须是非空对象")
        reports: dict[str, bool] = {}
        for prefix, aliased in raw_reports.items():
            if not isinstance(prefix, str):
                raise bad(f"`reports` 的键必须是字符串，得到 {prefix!r}")
            try:
                ipaddress.ip_network(prefix, strict=False)
            except ValueError as exc:
                raise bad(f"`reports` 的前缀不是合法网络：{prefix!r}") from exc
            if not isinstance(aliased, bool):
                raise bad(f"`reports[{prefix!r}]` 必须是 JSON 布尔")
            reports[prefix] = aliased
        return Action(
            kind="final_report",
            reason=reason,
            reports=MappingProxyType(reports),
        )

    prefix = payload["prefix"]
    if not isinstance(prefix, str):
        raise bad(f"`prefix` 必须是字符串，得到 {type(prefix).__name__}")
    try:
        ipaddress.ip_network(prefix, strict=False)
    except ValueError as exc:
        raise bad(f"`prefix` 不是合法网络：{prefix!r}") from exc

    if kind == "report":
        aliased = payload["aliased"]
        if not isinstance(aliased, bool):
            raise bad(
                f"`aliased` 必须是 JSON 的 true / false，得到 {aliased!r}"
                f"（{type(aliased).__name__}）。**1 / 0 不接受**：收下 1 意味着"
                "某一天有人写成 2 也会被收，而 `aliased_caught` 算的是布尔。"
            )
        return Action(kind="report", reason=reason, prefix=prefix, aliased=aliased)

    count = payload["count"]
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise bad(f"`count` 必须是正整数，得到 {count!r}")
    # **预算够不够不在这里判。** 那是 `BudgetLedger` 的职责（runner 侧），
    # 而"一个规则两处实现"正是本仓库反复在防的漂移。这里只管形态。
    return Action(kind="probe", reason=reason, prefix=prefix, count=count)


# ── 状态 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Stage1State:
    """策略在 Stage 1 自己攒的状态。**它进缓存键，所以字段全按构造有序无关。**

    字段逐个交代它是怎么做到与到达顺序无关的：

    | 字段 | 为什么序无关 |
    |---|---|
    | `prefixes` | 构造时 `sorted(set(...))`，**集合语义**，与传入顺序无关 |
    | `epoch` | 逻辑序号（`:564` 明确要求「逻辑序号而非墙钟」） |
    | `remaining_budget` | 标量 |
    | `probes` / `replies` / `failures` | **加法**合并，交换律 |
    | `hop_limits` | `frozenset`，集合语义 |
    | `indirect_seen` | 布尔**或** |
    | `reported` | 映射，`canonical()` 按键排序 |

    **没有任何一个字段是 `list`。** `canonical.canonical()` 会直接拒掉 `list`，
    而这里的每个字段都是"想清楚之后"的形态——两边是同一件事的两端。

    > Stage 1 的状态**刻意只有聚合量**，没有逐地址明细。理由不是省事：C5 的
    > 跨前缀记忆改的正是 **config 形态**（`:565`），那属 Stage 2 的记忆族（T6c），
    > 而 T6c 的第一件事**是补定义**、补完前不得开工。在这里先把逐地址明细做进
    > 状态里，等于替 T6c 预先决定了它要测什么。
    """

    prefixes: tuple[str, ...]
    epoch: int
    remaining_budget: int
    probes: Mapping[str, int]
    replies: Mapping[str, int]
    failures: Mapping[str, int]
    hop_limits: frozenset[int]
    indirect_seen: bool
    reported: Mapping[str, bool]

    # ── 构造 ──────────────────────────────────────────────────────────

    @classmethod
    def initial(cls, *, prefixes: Iterable[str], budget: int) -> Stage1State:
        """开局状态：全部计数为 0。

        `prefixes` **在这里去重并排序**，所以「传入顺序不同」不可能渗进键里。
        同时校验每个前缀语法合法——一个拼错的前缀若不在这里拦，会在 `observe()`
        里表现成「回包匹不上任何前缀」，那是个更难查的错。
        """
        ordered = tuple(sorted(set(prefixes)))
        for prefix in ordered:
            try:
                ipaddress.ip_network(prefix, strict=False)
            except ValueError as exc:
                raise DecisionError(f"前缀 {prefix!r} 不是合法网络") from exc
        zeros = MappingProxyType({prefix: 0 for prefix in ordered})
        return cls(
            prefixes=ordered,
            epoch=0,
            remaining_budget=budget,
            probes=zeros,
            replies=zeros,
            failures=zeros,
            hop_limits=frozenset(),
            indirect_seen=False,
            reported=MappingProxyType({}),
        )

    # ── 派生 ──────────────────────────────────────────────────────────

    def prefix_for(self, address: str) -> str:
        """最长前缀匹配：`address` 属于哪个前缀。

        **本模块自带这份匹配，不 import `src/scoring/`** —— 后者必须有真值
        （见 `src/scoring/__init__.py`），而本模块跑在**策略进程**里
        （`strategy_child.py`），导入它等于把真值拖进策略侧命名空间，
        直接违反 D7。「复用一个已有函数」在这里是**不能**做的优化。

        Raises:
            DecisionError: `address` 不落在任何一个前缀里。
        """
        try:
            addr = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DecisionError(f"{address!r} 不是合法地址") from exc
        best: str | None = None
        best_len = -1
        for prefix in self.prefixes:
            network = ipaddress.ip_network(prefix, strict=False)
            if network.version != addr.version:
                continue
            if addr in network and network.prefixlen > best_len:
                best, best_len = prefix, network.prefixlen
        if best is None:
            raise DecisionError(
                f"地址 {address} 不落在任何在册前缀里（{list(self.prefixes)}）。"
                "策略只应在册前缀内探测；出现这个错说明探到了界外，属 runner 侧 bug。"
            )
        return best

    # ── 推进（每个都返回新对象）────────────────────────────────────────

    def observe(self, responses: Iterable[Response]) -> Stage1State:
        """把一批回包并进计数。**这个方法是 bug 2 的修法在状态侧的落点。**

        参数是 `Iterable`，而且**合并只做加法和或**，所以「先到先合并」
        与「全收齐再合并」得到同一个对象——这正是并发下缓存键稳定的意思。

        **`send_failed` 也算一次探测**：三态里它是一次真实发生过的尝试
        （`:338`），把它从 `probes` 里漏掉会让「收到响应常常是坏消息」（`:138`）
        这个现象在状态里看不见。
        """
        probes = dict(self.probes)
        replies = dict(self.replies)
        failures = dict(self.failures)
        hops = set(self.hop_limits)
        indirect = self.indirect_seen
        for response in responses:
            prefix = self.prefix_for(response.dst)
            probes[prefix] = probes.get(prefix, 0) + 1
            if response.status == "received":
                replies[prefix] = replies.get(prefix, 0) + 1
            elif response.status == "send_failed":
                failures[prefix] = failures.get(prefix, 0) + 1
            if response.hop_limit is not None:
                hops.add(response.hop_limit)
            if response.indirect:
                indirect = True
        return replace(
            self,
            probes=MappingProxyType(probes),
            replies=MappingProxyType(replies),
            failures=MappingProxyType(failures),
            hop_limits=frozenset(hops),
            indirect_seen=indirect,
        )

    def with_budget_left(self, budget_left: int) -> Stage1State:
        """更新策略**自己那份**预算视图。

        它**不是**权威。权威在 runner 侧的 `BudgetLedger`（`TestBudgetIsNotLocal`
        断言的就是这件事：策略改自己这份视图不影响评分）。
        """
        return replace(self, remaining_budget=budget_left)

    def with_report(self, prefix: str, *, aliased: bool) -> Stage1State:
        """记下一个已提交的报告。覆盖同一个前缀是允许的（改主意是决策的一部分）。"""
        if prefix not in self.probes:
            raise DecisionError(f"报告了不在册的前缀 {prefix!r}")
        reported = dict(self.reported)
        reported[prefix] = aliased
        return replace(self, reported=MappingProxyType(reported))

    def next_epoch(self) -> Stage1State:
        """epoch 序号 +1。**这是唯一的"时间"，而它是逻辑序号，不是墙钟。**"""
        return replace(self, epoch=self.epoch + 1)

    # ── 两个出口 ──────────────────────────────────────────────────────

    def canonical_value(self) -> dict[str, Any]:
        """进缓存键的形态。**只有这一个出口进键。**

        全部字段都是基本类型 / 元组 / `frozenset` / 映射，于是
        `canonical.canonical()` 必然接受（`test_canonical.py` 里有断言钉住
        "状态永远可规范化"，否则将来往状态里加一个字段就可能悄悄让键算不出来）。
        """
        return {
            "prefixes": self.prefixes,
            "epoch": self.epoch,
            "remaining_budget": self.remaining_budget,
            "probes": dict(self.probes),
            "replies": dict(self.replies),
            "failures": dict(self.failures),
            "hop_limits": self.hop_limits,
            "indirect_seen": self.indirect_seen,
            "reported": dict(self.reported),
        }

    def render(self) -> str:
        """给模型看的文本。**纯函数、逐行确定**——它是 prompt 的一部分，
        而 prompt 的字节进缓存键（`cache.py` 的 `prompt` 分量）。"""
        lines = [f"前缀（{len(self.prefixes)} 个，按字典序）："]
        for prefix in self.prefixes:  # 构造时已排序
            line = (
                f"- {prefix}：已探 {self.probes.get(prefix, 0)}，"
                f"收到 {self.replies.get(prefix, 0)}，"
                f"发包失败 {self.failures.get(prefix, 0)}"
            )
            if prefix in self.reported:
                line += f"，已报告 aliased={str(self.reported[prefix]).lower()}"
            lines.append(line)
        lines.append(f"回包 hop limit 取值集合：{sorted(self.hop_limits)}")
        lines.append(f"是否存在 src != dst 的回包：{self.indirect_seen}")
        lines.append(f"剩余预算：{self.remaining_budget}")
        lines.append(f"决策 epoch 序号：{self.epoch}")
        return "\n".join(lines)


# ── 决策 ──────────────────────────────────────────────────────────────────

DecisionSource = Literal["cache", "model"]


@dataclass(frozen=True, slots=True)
class Decision:
    """一次决策的完整记录。

    `prompt` 与 `raw` 都留着：**报告里能贴出「它看到了什么、它说了什么」**，
    这正是 P1 那条「决策理由 | 可读」在数据侧的样子。`source` 是
    `CacheStats.hit_rate` 之外的第二条观测线——判据 6(ii) 要求缓存命中率
    100%（`:739`），而"哪几次是打 API 拿的"必须能被逐条点名。
    """

    action: Action
    raw: str
    key: CacheKey
    prompt: str
    source: DecisionSource

    @property
    def cache_hit(self) -> bool:
        return self.source == "cache"


def decide(
    *,
    state: Stage1State,
    tool_pool: ToolPool,
    seed: int,
    config: Mapping[str, Any],
    cache: DecisionCache | None = None,
    completion: Completion | None = None,
) -> Decision:
    """做一次决策：渲染 prompt → 查缓存 → 未命中则问模型 → **校验**。

    Args:
        state: 当前状态。**它的 `epoch` 由调用方推进**（`next_epoch()`），
            本函数不推进——推进是循环的事。
        tool_pool: 工具池。它的内容就是 A1/A2 的自变量。
        seed: 这一轮的种子。配对单位是 `(config, seed)` 而不是 `state`
            （`:559`），所以 seed 单独入键、不藏在 state 里。
        config: **整份**配置（含六项 LLM 身份）。不得传路径。
        cache: 决策缓存。`None` = 一次性的内存缓存（单测用）。
        completion: 注入口。**`None` = 离线回放模式**：任何未命中都抛
            `DecisionError`，绝不偷偷联网。见模块 docstring。

    Raises:
        DecisionError: 未命中且没有 `completion`，或 `config` 缺身份字段。
        MalformedDecision: 模型（或缓存里存的原文）不是合法动作。
    """
    require_llm_identity(config)

    prompt_text = prompts.render(tools=tool_pool.as_pairs(), state_text=state.render())
    key = CacheKey.of(
        state=state.canonical_value(),
        seed=seed,
        prompt=prompt_text,
        tool_pool=tool_pool.as_mapping(),
        config=config,
    )

    store = cache if cache is not None else DecisionCache()
    raw = store.lookup(key)
    source: DecisionSource = "cache"
    if raw is None:
        if completion is None:
            raise DecisionError(
                "决策缓存未命中，且没有提供 completion（离线回放模式）。\n"
                f"缺的键分量：{dict(key.components)}\n"
                "**这里必须炸，不能联网。** 回放时偷偷打一次 API 得到的是"
                "一次新的、没人记录过的调用，而报告里写着它是复现。"
            )
        raw = completion(prompt_text)
        if not isinstance(raw, str):
            raise DecisionError(
                f"completion 必须返回字符串（模型的原始输出文本），"
                f"得到 {type(raw).__name__}"
            )
        # **先存原文再校验**：校验失败时那份畸形输出也必须进缓存。
        # 否则回放会重新问一次模型，而模型完全可以给出另一个答案——
        # 于是"复现"复现出了另一条轨迹，且没有任何一处会红。
        store.store(key, raw)
        source = "model"

    action = parse_action(raw, origin="缓存中的原文" if source == "cache" else "模型输出")
    return Decision(action=action, raw=raw, key=key, prompt=prompt_text, source=source)


__all__ = [
    "LLM_IDENTITY_KEYS",
    "CONSTRUCTION_BLUEPRINTS",
    "Action",
    "ActionKind",
    "Completion",
    "Decision",
    "DecisionError",
    "DecisionSource",
    "MalformedDecision",
    "Stage1State",
    "Tool",
    "ToolPool",
    "decide",
    "parse_action",
    "require_llm_identity",
]
