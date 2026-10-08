"""config → `Stage1Truth` 导出（T3）—— 靶场配置**就是** Stage 1 的真值。

设计方案 `:626`：「Punching Bag 的 JSON 配置**就是** ground truth，直接读它
导出 `GroundTruth`，不需要另建标注流程。」

---

## 一、承重的那条映射：`aliased` 不在配置里

config 里只有 `ipv6_prefix` 与四档响应率（`default` / `EUI` / `lower` /
`higher`），**没有 `aliased` 字段**。它是我们推出来的：

> 该前缀下**每一个**地址都回包　⟺　四档速率**都**满足「必回」

三段理由，缺一段这个等价就不成立：

1. **「每一个地址都回包」就是 aliased / FRP 的定义**（设计方案 `:135`：
   「某个前缀下**每一个**地址都响应」）。
2. **四档互斥且穷尽全部地址**：靶场对每个回包地址按 `isEUI64` →
   `isLowerByte` → `isHigherByte` → `default` 的顺序取一档
   （`IPv6-trie.hpp:321-331`），最后一档是兜底。所以「每档都必回」
   ⟺「每个地址都回」，中间没有漏掉的类别。
3. **「必回」不是 `rate == 1.0`，而是 `rate * 1000 > 999`。** 回包判据是
   `hash_ipv6_fast(dst) % 1000 < rate * 1000`（`punchingbag.cpp:147-157`），
   而 `hash % 1000 ∈ [0, 999]`。于是阈值只要 > 999 就**恒真**：`1.0` 满足、
   `0.9995` 也满足，而 `0.999` **不**满足（阈值恰好 999.0，`random_value`
   等于 999 的那千分之一地址不回）。见 `always_replies()`。
   > 这一条不是学究：确定性判据是 `random_responses == false` 时的默认路径，
   > 而我们的 compose **没有**传 `--random-responses`，所以走的正是这条。

**这条映射是我们自己定的、且承重**——设计方案 `:979` 自己承认：Stage 1 的
「config = 真值，无循环性」把循环性从「标签」搬到了「**解释**」。既如此，
它必须只写在**一处**（`SubnetRates.is_frp`），并且边界有测试钉住
（`tests/unit/test_ground_truth.py::TestAliasingBoundary`）。

### 一处已知的偏离：档位可达性

「四档都必回」与「每个地址都必回」在**档位不可达**时不严格等价：`lower` /
`higher` 两档在 `prefix_len <= 10` 与 `prefix_len == 128` 时**取不到**
（`IPv6-trie.hpp:244` / `:256` 的前置判断），此时那两档写什么都没用。
本模块**按要求逐档判**，不建模可达性——因为 Stage 1 的 config 用 /48~/64
（T13 定），两者在这些长度上**结论相同**；而建模它会把一条能一眼看懂的
规则变成一段要跟着 C++ 改的仿真。**若将来有 config 用到 `::/0` 或 /120 以上
的前缀，这条偏离要重新审。**（`::/0` 见下方 `::/0` 那段。）

---

## 二、「未配置前缀」与「rate==0 前缀」是两个集合（本任务存在的理由）

失败模式表 `:1174` 点名的这一条，具体是这样发生的：

| | 在 config 里？ | 会回包？ |
|---|---|---|
| aliased 前缀 | 是 | 是（每个地址都回） |
| 有内容的前缀 | 是 | 是（按速率，一部分回） |
| **`rate==0` 的前缀** | **是** | **否** |
| **不在 config 里的前缀** | **否** | **否** |

后两行在**观测上完全同形**（都不回包），在实验语义上是两件事：前者是
「实验建模了、但它沉默」，后者是「**真值里根本没有它的答案**」。

危险不在这个区分本身，而在它**会被静默抹掉**。一句最常见的写法就抹掉了：

```python
aliased = truth.prefixes.get(prefix, {"aliased": False})["aliased"]   # ← 别这么写
```

`dict.get` 的默认值把「不在 config 里」悄悄读成了「不是 aliased」——
**那是在替真值编答案**，而编出来的答案（False）看起来与正确答案同形。

所以本模块的做法是让**配置过**这件事本身能被问出来，而不是靠 `aliased`
的真假去反推：

* `stage1_truth_from_config()` 产出的 `prefixes` 里**含全部配置过的前缀**，
  包括四档全 0 的那些——它们是「沉默的」，不是「不存在的」；
* `SubnetTable.configured` / `.responsive` 是两个**不同的集合**，
  差集**恰好**是 `rate==0` 的那些，可以直接断言（见 `TestTwoSets`）；
* 真值回答不了「在不在 config 里」这个问题，**这是设计如此**——
  `Stage1Truth` 只有一个 `aliased` 字段。要那个答案就必须拿 `SubnetTable`，
  而拿到它的地方（runner 侧）本来就有 config。

### `::/0` 的特殊之处

靶场自带的六份 config 全部用 `"ipv6_prefix": "::/0"` 当唯一的 subnet。
那时「不在 config 里」是**空集**（每个地址都落在 `::/0` 里），
上表后两行只剩一行。这不影响本模块的逻辑，但会让「未配置前缀」这条断言
**测不到东西**——所以 `TestTwoSets` 用的是分段前缀的 config，不是 `::/0`。
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .truth import Stage1Truth

#: 接受三种来源：已经在内存里的 dict、config 的路径、路径的字符串形式。
ConfigSource = "Mapping[str, Any] | str | Path"

#: 回包判据 `hash(dst) % 1000 < rate * 1000` 里的桶数（`punchingbag.cpp:154`）。
_HASH_BUCKETS = 1000


class ConfigError(ValueError):
    """配置**本身**有问题，靶场的行为会与真值导出分叉。

    与「问了一个 config 里没有的前缀」是两件事：那是查询未命中，这是
    config 不能用来当真值。分开一个类型，是因为调用方对两者的反应不同——
    前者该走「不在建模范围内」的分支，后者该停下来修配置。
    """


class _SkipSubnet(Exception):
    """内部信号：这条 subnet 靶场也会跳过，所以我们也跳过。

    不对外暴露——跳过的条目会进 `SubnetTable.skipped`，而
    `stage1_truth_from_config()` 见到它就拒绝出真值。
    """


def always_replies(rate: float) -> bool:
    """该档速率是否让**每一个**落进这一档的地址都回包。

    **不是** `rate == 1.0`。回包判据是
    `random_value < rate * 1000`，而 `random_value = hash(dst) % 1000`
    取值于 `[0, 999]`（`punchingbag.cpp:147-157`）。所以只要阈值 `> 999`
    就恒真：`1.0 → 1000.0` ✓、`0.9995 → 999.5` ✓、`0.999 → 999.0` ✗。

    > **精度**：C++ 把 `rate * 1000` 存进一个 32 位 `float`（`:147`），
    > 这里按 double 算。二者只在距离阈值约 `6e-5` 以内时可能不一致——
    > 那不是人手写得出来的速率，所以没有模拟 float32。
    """
    return rate * _HASH_BUCKETS > _HASH_BUCKETS - 1


@dataclass(frozen=True, slots=True)
class SubnetRates:
    """一个 subnet 的四档响应率，**原样**保留。

    不做归一化、不丢字段：真值的推导要能看到全部四档，否则「为什么这个前缀
    算 aliased」就变成了一句没法复核的话。缺字段的语义与靶场一致——
    `subnet.value("...", 0.0)`（`IPv6-trie.hpp:217-220`），即**缺省为 0**，
    而 0 意味着这一档的地址一个都不回。
    """

    default: float = 0.0
    eui: float = 0.0
    lower: float = 0.0
    higher: float = 0.0

    @property
    def all_rates(self) -> tuple[float, float, float, float]:
        """四档的取值，顺序与 `IPv6-trie.hpp:321-331` 的判定顺序一致。"""
        return (self.default, self.eui, self.lower, self.higher)

    @property
    def is_frp(self) -> bool:
        """**本模块承重的那一条**（见模块 docstring 第一节）。

        aliased ⇔ 四档都必回。改这里等于改真值的定义——改了要同步
        设计方案 `:135` / `:626` / `:979` 三处，并复跑 `TestAliasingBoundary`。
        """
        return all(always_replies(r) for r in self.all_rates)

    @property
    def is_silent(self) -> bool:
        """四档全 0 —— **配置了，但它一个地址都不回**。

        它与「不在 config 里」在观测上同形，在真值里必须不同：见模块
        docstring 第二节。上界用 `<= 0.0` 而不是 `== 0.0`，因为负速率在
        靶场里同样永远小于阈值（不构成合法配置，`read_subnets` 会拒掉）。
        """
        return all(r <= 0.0 for r in self.all_rates)

    @property
    def can_reply(self) -> bool:
        """存在至少一个会被回包的地址。`is_silent` 的补集。"""
        return not self.is_silent


@dataclass(frozen=True, slots=True)
class SubnetTable:
    """config 里**逐条**读出来的东西 —— 包括靶场照收但从不回包的那些。

    这张表与 `Stage1Truth` 的差别**正是 T3 存在的理由**：真值只回答
    「是不是 aliased」，而这张表还回答「它到底在不在 config 里」。
    """

    #: 规范化后的前缀（`ipaddress` 会掩掉主机位，与靶场只比较前 `prefix_len`
    #: 位的行为一致）→ 四档速率。**含四档全 0 的条目。**
    subnets: dict[str, SubnetRates]
    #: 被跳过的条目（含位置与原因）。**靶场也是跳过的**——缺 `ipv6_prefix`
    #: 时 `continue`（`IPv6-trie.hpp:210`），前缀解析失败或速率不是数时
    #: `catch` 掉只打一行 stderr（`:223-225`）。这里必须同样跳过，否则我们与
    #: 靶场对**同一份 config** 的理解就分叉了。但**不静默**：
    #: `stage1_truth_from_config()` 见到它就拒绝出真值。
    skipped: tuple[str, ...] = ()
    #: 同一个前缀被写了两次、而两次的速率**不同**。靶场的 `insert` 返回已有
    #: 节点、`setRates` 直接覆盖 → **后者胜**（`:215-221`）。我们照做，
    #: 但也不静默：写 config 的人多半以为两条都生效了。
    conflicting: tuple[str, ...] = ()

    @property
    def configured(self) -> frozenset[str]:
        """config 里出现过的**全部**前缀——包括沉默的那些。"""
        return frozenset(self.subnets)

    @property
    def responsive(self) -> frozenset[str]:
        """存在会被回包的地址的前缀。**这才是「会回包」的集合。**"""
        return frozenset(p for p, r in self.subnets.items() if r.can_reply)

    @property
    def silent(self) -> frozenset[str]:
        """配置了、但一个地址都不回的前缀。`configured - responsive`。"""
        return frozenset(p for p, r in self.subnets.items() if r.is_silent)

    @property
    def aliased(self) -> frozenset[str]:
        """`is_frp` 为真的前缀——`aliased_waste` 的分子。"""
        return frozenset(p for p, r in self.subnets.items() if r.is_frp)


def _normalize_prefix(raw: Any, where: str) -> str:
    """把 config 里的一条 `ipv6_prefix` 变成规范 CIDR。

    靶场 `parseIPv6`（`IPv6-trie.hpp:58-83`）要求有 `/`、长度 ≤ 128、且能被
    `inet_pton` 解析，三者任一不满足都算失败。`ipaddress` 的 `strict=False`
    会掩掉主机位，而靶场只比较前 `prefix_len` 位（`:290-295`），效果相同。
    """
    if not isinstance(raw, str):
        # 注意：这一条**不**按「靶场也跳过」处理。靶场那行 `std::string
        # prefixStr = subnet["ipv6_prefix"];`（`:212`）在 try **外面**，
        # 所以非字符串会让它直接抛出去、起不来。我们也拒。
        raise ConfigError(f"{where}: ipv6_prefix 不是字符串（{raw!r}）")
    if "/" not in raw:
        # **这一句不能省。** 靶场 `parseIPv6` 的第一句就是找 `/`，找不到直接抛
        # （`IPv6-trie.hpp:60-61`），于是整条 subnet 被 catch 掉。而
        # `ipaddress.ip_network("2001:db8::")` **不会报错**——它返回
        # `2001:db8::/128`，一条主机路由。两者对同一份 config 的理解就此分叉，
        # 而且分叉的方向很坏：我们会多出一个 `/128` 的前缀（它从不回包，
        # 于是静默地混进「沉默的前缀」那一类）。
        raise _SkipSubnet(f"ipv6_prefix={raw!r} 缺 '/' —— 靶场也跳过")
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError as exc:
        raise _SkipSubnet(f"ipv6_prefix={raw!r} 解析失败：{exc}") from exc


def _rate(subnet: Mapping[str, Any], key: str) -> float:
    """读一档速率。缺省 0.0（与 `subnet.value(key, 0.0)` 同）。

    三类失败分开处理，因为**靶场对它们的反应不同**：
      * 不是数（含 JSON 的 `true`——nlohmann 的 `value<double>` 收不下 bool）
        → 靶场 `catch` 掉整条 subnet，我们也跳过；
      * 是数但越界 → 靶场**照收**，所以这里必须**拒**：跳过就等于我们
        悄悄丢了一个靶场会照做的前缀，那是分叉，比分叉更糟的是它静默。
    """
    raw = subnet.get(key, 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _SkipSubnet(f"{key}={raw!r} 不是数")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ConfigError(
            f"{key}={value!r} 越界；靶场的响应率是概率（0.0–1.0，见上游 README）。"
            "靶场会照收这个值并把它当『总是回』处理，我们拒——"
            "否则真值是从一个非法配置里推出来的"
        )
    return value


def _load(source: Any) -> Mapping[str, Any]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except FileNotFoundError as exc:
            raise ConfigError(f"config 不存在：{path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config 不是合法 JSON：{path}（{exc}）") from exc
    elif isinstance(source, Mapping):
        loaded = source
    else:
        raise TypeError(f"config 来源必须是路径或 Mapping，得到 {type(source).__name__}")

    if not isinstance(loaded, Mapping):
        raise ConfigError(f"config 顶层必须是对象，得到 {type(loaded).__name__}")
    return loaded


def read_subnets(source: Mapping[str, Any] | str | Path) -> SubnetTable:
    """逐条读 config 的 `subnets`，**不做任何取舍**。

    它比 `stage1_truth_from_config()` 更宽：跳过的与冲突的条目都如实记下来，
    交给调用方决定怎么办。想要一个「能不能用」的判决就用后者。

    Raises:
        ConfigError: 顶层形状不对（缺 `subnets` / 不是数组——靶场在
            `IPv6-trie.hpp:205` 也在这里抛）、`ipv6_prefix` 不是字符串、
            或某档速率越界。
    """
    document = _load(source)

    if "subnets" not in document or not isinstance(document["subnets"], list):
        # 靶场原话：`JSON does not contain a valid 'subnets' array`（`:206`）。
        raise ConfigError("config 里没有合法的 'subnets' 数组")

    subnets: dict[str, SubnetRates] = {}
    seen: dict[str, list[SubnetRates]] = {}
    skipped: list[str] = []

    for index, entry in enumerate(document["subnets"]):
        where = f"subnets[{index}]"
        if not isinstance(entry, Mapping):
            skipped.append(f"{where}: 不是对象（{entry!r}）")
            continue
        if "ipv6_prefix" not in entry:
            # 靶场：`if (!subnet.contains("ipv6_prefix")) continue;`（`:210`）
            skipped.append(f"{where}: 缺 ipv6_prefix —— 靶场也跳过")
            continue

        try:
            prefix = _normalize_prefix(entry["ipv6_prefix"], where)
            rates = SubnetRates(
                default=_rate(entry, "default_response_rate"),
                eui=_rate(entry, "EUI_response_rate"),
                lower=_rate(entry, "lower_response_rate"),
                higher=_rate(entry, "higher_response_rate"),
            )
        except _SkipSubnet as exc:
            skipped.append(f"{where}: {exc} —— 靶场也跳过")
            continue

        # 重复前缀：后者胜（靶场 `insert` 复用了节点，`setRates` 覆盖）。
        seen.setdefault(prefix, []).append(rates)
        subnets[prefix] = rates

    conflicting = tuple(
        sorted(p for p, rs in seen.items() if len(set(rs)) > 1)
    )
    return SubnetTable(
        subnets=subnets, skipped=tuple(skipped), conflicting=conflicting
    )


def stage1_truth_from_config(
    source: Mapping[str, Any] | str | Path,
) -> Stage1Truth:
    """config → `Stage1Truth`（runner 侧喂给 `Oracle` 的那个可调用对象）。

    **`prefixes` 里含全部配置过的前缀，包括四档全 0 的那些。** 它们是
    「沉默的」而不是「不存在的」——把前者从真值里删掉，落在它们上面的探测
    就变得**无法归属**，而「无法归属」在下游会静默退化成「不是 aliased」，
    正是本模块第二节要堵的那条路。

    Raises:
        ConfigError: config 里有靶场会跳过、或自相矛盾的条目。**这里必须
            炸而不是忍**：靶场跳过一条前缀时**不留任何痕迹**，于是
            「我配了 50 个前缀」与「实际生效 49 个」在两边都一样跑得下去，
            而这个差会直接改 `aliased_waste_ratio` 的分母。
    """
    table = read_subnets(source)

    if table.skipped:
        raise ConfigError(
            f"config 里有 {len(table.skipped)} 条被靶场跳过的 subnet"
            "（靶场不报错、也不留痕，所以这里必须拦住）：\n  "
            + "\n  ".join(table.skipped)
        )
    if table.conflicting:
        raise ConfigError(
            "同一个前缀被写了两次且速率不同，靶场按『后者胜』静默取一条：\n  "
            + "\n  ".join(table.conflicting)
        )

    return Stage1Truth(
        prefixes={p: {"aliased": r.is_frp} for p, r in table.subnets.items()}
    )


__all__ = [
    "ConfigError",
    "SubnetRates",
    "SubnetTable",
    "always_replies",
    "read_subnets",
    "stage1_truth_from_config",
]
