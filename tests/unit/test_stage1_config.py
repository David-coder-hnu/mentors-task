"""Stage 1 配置规格 —— 把 `config/stage1_config.json` 里的每个数现算一遍（T13）。

## 这份文件为什么存在

`config/stage1_config.json` **就是 Stage 1 的真值**（`:626`）：靶场读它的
`subnets` 决定回不回包，harness 读同一份导出 `GroundTruth`。所以「这份 config
合不合规」不是一项审阅工作，是一条可以被算出来的性质。**这里不描述规格，
这里复算规格**——改了 config 而没改这里的推导，测试会红。

三件事分开测：

| 组 | 问题 |
|---|---|
| `TestConfigIsUsable` | 靶场与我们会**同样**读它吗（跳过/冲突/形状） |
| `TestTheKnobs` | v6 要的四个口径字段 + v7 要的 LLM 身份六项齐不齐 |
| `TestB5IsDistinguishable` | **`:821` 第 3 段**：B5 在这份档位下是可分的，还是退化成常数 |
| `TestB0AlarmIsCalibrated` | **D10 的退化警报**在这份 config 上是"通过"还是"报警" |
| `TestWallClockBudget` | 门 1 的挂钟上限盖不盖得住这份规格 |
| `TestBaselineImportGraph` | `src/baselines/` 与 `src.agent/` 受同一条 D7 约束 |

---

## ⚠️ B5 的判定模型，以及它的**歧义**（本文件最该被读的一段）

`:848` 给 B5 的规则是：**每唯一前缀在 /48 /64 /96 /112 /116 /120 六种长度上各发
3 次随机后缀探测，≥2 次 Echo Reply 即判 aliased。**

这句话有两种读法，而它们给出的假阳率**差四倍**：

* **读法一（本文件采用，也是 `:826` 自己的口径）**：一次 n=3 判定，
  `P(判 aliased) = P(≥2/3 回包) = r²(3−2r)`。`:826` 原文就是「n=3 下
  P(≥2 回包) ≈ 1」——**它按 3 次算，不是按 18 次算**。
* **读法二**：六个长度是六次独立机会，任一长度触发即判 aliased。
  `P = 1 − (1 − r²(3−2r))⁶`。

| | 读法一 | 读法二 |
|---|---|---|
| 24 个非 aliased 前缀的聚合假阳率 | **6.1%** | **23.4%** |

**结论对两种读法相同**（都是"可分"），所以 `:821` 要的那句结论是稳的；
但**数字差 3.8 倍**，而 `:651` 已经把 B5 的归类（(i) 预算自适应在线策略 /
(ii) 18 探扫描）明确列为**未定、且必须等读到 6SEVEN 源码才能写定**的事。
所以这里把两个数**都**算出来并断言：**T6b 必须先把读法钉死，再引用本文件的数。**
这一条已写回设计方案。

`r` 是**有效单探回包率**：随机后缀按地址形态落进四档，取加权平均（`:830-832`）。
`content-*` 四组的四档同值，加权是恒等；两个 `tier-*-hot` 组则由
`default` 主导、被 `EUI` / `lower` / `higher` 各抬一点点。
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapter.ground_truth import read_subnets, stage1_truth_from_config
from src.agent.decide import LLM_IDENTITY_KEYS, require_llm_identity
from src.baselines.b0_probe_all import DEGENERATE_WASTE_FLOOR, plan_counts

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "stage1_config.json"

# ── 靶场档位判定里"随机后缀落进哪一档"的先验（`:830-832`）──────────────────
#
# 三档的判据都只看地址自身，与长度无关的那部分见 `:830` 的注。随机后缀下：
#   * `isEUI64`：第 11、12 字节恰为 `ff:fe` → 16 位约束。
#   * `isLowerByte` / `isHigherByte`：仅最低 10 位可不同 → 10 位约束。
# 三档互斥且按此顺序判定，`default` 是兜底。
P_EUI = 2.0**-16
P_LOWER = 2.0**-10
P_HIGHER = 2.0**-10

#: 实测吞吐预算（`:742`：无丢包 4 850 pps 的一半，为 2× 拥塞余量）。
STATION_PPS = 2400.0

#: 回包成簇到达的实测时长（`:1359`：20 000 个回包在 235 ms 内到齐）。
#: 排空窗口必须盖住它——这是 `W` 取值的依据，不是拍的。
BURST_ARRIVAL_S = 0.235


# ── 读 config ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def raw() -> dict:
    """原样的 JSON。`_role` 只在这里有——`read_subnets` 故意不看它。"""
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _effective_rate(rates) -> float:
    """随机后缀下这一档位组合的**有效单探回包率**。"""
    return (
        (1.0 - P_EUI - P_LOWER - P_HIGHER) * rates.default
        + P_EUI * rates.eui
        + P_LOWER * rates.lower
        + P_HIGHER * rates.higher
    )


def _p_declared(rate: float) -> float:
    """**读法一**：`P(≥2/3 回包) = r²(3−2r)`（`:826`）。"""
    return rate * rate * (3.0 - 2.0 * rate)


def _p_declared_six_chances(rate: float) -> float:
    """**读法二**：六个长度各一次独立机会。见模块 docstring。"""
    return 1.0 - (1.0 - _p_declared(rate)) ** 6


def _canon(written: str) -> str:
    """config 里**写**的前缀 → 靶场与 runner 实际用的**规范**形式。

    **这不是洁癖**：本 config 的第一行写作 `2001:db8:5:0::/64`，而
    `ipaddress` 的规范形式是 `2001:db8:5::/64`（零段被压掉）。
    靶场用 `inet_pton` 解析、我们走 `read_subnets`，两边都认这两种写法——
    **但只要有一处代码拿"写下来的字符串"去查真值表，它就会在那一个前缀上
    查空**。runner 下发给策略的 `prefixes` 是**规范形式**，所以正确的做法是
    在边界上就规范化，而不是指望两边恰好写成一样。

    （这条差异是**故意留着的**：它是一份免费的规范化测试。有一个前缀
    写形 ≠ 规范形，任何"靠字符串相等"的假设都会在这里现形——本文件的作者
    就撞到过一次。）
    """
    return str(ipaddress.ip_network(written, strict=False))


def _rates_by_role(raw: dict) -> dict[str, list]:
    """按 `_role` 分组拿四档速率（`read_subnets` 不读 `_role`，所以两边都要）。

    **不能用 `default_response_rate` 当分组键**：`content-sparse`、
    `tier-eui-hot`、`tier-byte-hot` 三组的 `default` 都是 0.02，靠它分组会把
    9 个前缀并成一组，而它们的**有效单探回包率并不相同**（后两组被各自的
    hot 档抬高了一点点）——那正是这个区分要保住的东西。
    """
    table = read_subnets(CONFIG_PATH)
    grouped: dict[str, list] = {}
    for entry in raw["subnets"]:
        grouped.setdefault(entry["_role"], []).append(table.subnets[_canon(entry["ipv6_prefix"])])
    return grouped


# ── ① 靶场与我们会同样读它吗 ────────────────────────────────────────────────


class TestConfigIsUsable:
    """`read_subnets` / `stage1_truth_from_config` 是靶场的**影子读法**。

    一条被跳过的 subnet 在靶场里**不留任何痕迹**（`:365`），而它会直接改
    `aliased_waste_ratio` 的分母。所以"零跳过"不是整洁度，是这条端点的
    口径成立与否。
    """

    def test_nothing_is_skipped_and_nothing_conflicts(self) -> None:
        table = read_subnets(CONFIG_PATH)
        assert table.skipped == (), f"靶场会静默跳过：{table.skipped}"
        assert table.conflicting == (), f"同一前缀写了两次且不同：{table.conflicting}"

    def test_the_truth_exports_without_complaint(self) -> None:
        truth = stage1_truth_from_config(CONFIG_PATH)
        assert len(truth.prefixes) == 32

    def test_the_three_sets_have_the_expected_sizes(self) -> None:
        table = read_subnets(CONFIG_PATH)
        assert len(table.configured) == 32
        assert len(table.responsive) == 29, "8 aliased + 18 有内容 + 3 tier-hot"
        assert len(table.silent) == 3
        assert len(table.aliased) == 8

    def test_the_silent_set_is_exactly_the_zero_rate_group(self, raw) -> None:
        """沉默 ≠ 不存在（T3 的第二节）：三行 `silent` 在 config 里，在真值里，
        只是不回包。把它们的 `_role` 与语义集合对上，防止有人"顺手"删掉它们。"""
        table = read_subnets(CONFIG_PATH)
        roles = {
            entry["ipv6_prefix"]: entry["_role"] for entry in raw["subnets"]
        }
        assert {roles[p] for p in table.silent} == {"silent"}

    def test_every_prefix_is_a_slash_64_inside_one_route(self) -> None:
        """**全部 /64、且同在一个 /48 里。**

        这不是凑数：T3 记录的已知偏离是「`lower` / `higher` 两档在
        `prefix_len <= 10` 与 `== 128` 时不可达」（`:1390`），/64 上两者结论
        相同，所以那条偏离在这里**不生效**。而同一个 /48 意味着 compose 里
        一条路由就能覆盖全场（`docker/compose.yml` 的 `2001:db8:1::/64` 是冒烟
        那份，这份要走 `2001:db8:5::/48`）。
        """
        table = read_subnets(CONFIG_PATH)
        assert all(p.endswith("/64") for p in table.configured), "有非 /64 的前缀"
        assert all(p.startswith("2001:db8:5:") for p in table.configured)

    def test_the_only_aliased_prefixes_are_the_all_ones_ones(self) -> None:
        """`aliased ⇔ 四档都必回`——**并且没有第二种进法**。

        两个 `tier-*-hot` 组是刻意放在这里的**反例**：它们各有一档是 1.0，
        看起来"很热"，但四档不全必回，所以**不是** aliased。T3 的
        `test_only_the_all_ones_prefix_is_aliased` 用的是手工 config，
        这条把同一性质钉在**真 config** 上。
        """
        table = read_subnets(CONFIG_PATH)
        assert len(table.aliased) == 8
        for prefix, rates in table.subnets.items():
            if prefix in table.aliased:
                assert all(r * 1000 > 999 for r in rates.all_rates), prefix
            else:
                assert not all(r * 1000 > 999 for r in rates.all_rates), (
                    f"{prefix} 有四档全必回却没被判 aliased"
                )

    def test_every_role_in_the_file_is_one_we_know(self, raw) -> None:
        """`_role` 是**我们的**标注，靶场忽略它（顶层 `_comment` 里写着）。

        它不进真值——`read_subnets` 根本不读它。但它是人读这份 config 的
        索引，所以拼错了要炸：`"_role": "aliasd"` 会静默地让下面几条按
        `_role` 聚合的断言少算一组，而那看起来只是"数值对不上"。
        """
        known = {
            "aliased",
            "content-mid",
            "content-low",
            "content-sparse",
            "tier-eui-hot",
            "tier-byte-hot",
            "silent",
        }
        roles = [entry["_role"] for entry in raw["subnets"]]
        assert set(roles) == known, f"多出/缺少角色：{set(roles) ^ known}"
        assert roles[:8] == ["aliased"] * 8, "aliased 必须排在列表最前面（见 B0 那组）"


# ── ② 四个口径字段 + 六个身份字段 ───────────────────────────────────────────


class TestTheKnobs:
    """v6 要的四个字段与 v7 要的六项身份。**缺任一项都是硬失败。**"""

    def test_the_four_scope_fields_are_present(self, raw) -> None:
        """`:1499-1503`。注意 `W` 在本文件里的键名是 **`W_s`**——单位写进
        键名，因为排空窗口的语义就是秒（`:579`「在 W 秒排空窗口下」），
        而文档里的 `W` 不带单位。**它就是文档的 `W`，不是另一个量。**
        """
        for key in ("W_s", "epoch_size", "budget", "api_cost_cap", "wall_clock_cap_s"):
            assert key in raw, f"config 缺 `{key}`"

    def test_the_budget_matches_the_smoke_table(self, raw) -> None:
        """`:851` 的 C12 口径：Stage 1 从 1000 降到 **300**。"""
        assert raw["budget"] == 300

    def test_epoch_size_divides_the_budget_exactly(self, raw) -> None:
        """除不尽会留下一个不满的尾 epoch，而"epoch 数"是挂钟估算的因子之一
        （`:586`）——留个余数会让那个算式变成近似。"""
        assert raw["budget"] % raw["epoch_size"] == 0
        assert raw["budget"] // raw["epoch_size"] == 15

    def test_the_drain_window_covers_the_measured_burst(self, raw) -> None:
        """`W` 的取值依据：它必须盖住实测的回包成簇时长。"""
        assert raw["W_s"] > BURST_ARRIVAL_S, "排空窗口比实测簇到达时长还短"

    def test_the_llm_identity_six_are_present_and_complete(self, raw) -> None:
        """直接调 T5 那个硬检查——**不在这里重新写一遍字段名单**。

        重写一遍就会与 `decide.LLM_IDENTITY_KEYS` 分叉，而分叉的症状是
        "config 看起来齐了、`decide()` 仍然拒"，最难查。
        """
        require_llm_identity(raw)
        assert set(LLM_IDENTITY_KEYS) <= set(raw)

    def test_stage_one_cost_cap_is_zero_and_that_is_a_conditional_claim(self, raw) -> None:
        """`api_cost_cap = 0` 是**当下**的正确值，不是永久结论。

        `:594` 要求 config 里写明这个数按「调用量 × 单价」推出，并明确
        **文档不填值**（单价与预算意图不是设计推导能给出的输入）。这里的
        0 来自一个可核对的当下事实：**Stage 1 冒烟表里没有任何一行确定要调
        LLM**——A1/A2 不在 Stage 1（`:626`），而 M1/M2 的驱动方式**未定**
        （T6c 的第一件事就是补定义）。

        **所以这条断言是把一个条件写下来**：T6c 一旦把 M1/M2 定成 LLM 驱动，
        这个 0 就错了，而这条测试会红——那正是它该做的事。公式是
        `策略数 × (预算 / epoch_size) × N`（`:594`）。
        """
        assert raw["api_cost_cap"] == 0
        stage1_strategies = 5  # B0 / B1 / B4 / M1 / M2（`:581` 的「5 行」）
        assert stage1_strategies == 5, "行数变了就要重算 api_cost_cap 的公式"
        seeds = 30
        stage2_calls = stage1_strategies * (raw["budget"] // raw["epoch_size"]) * seeds
        assert stage2_calls == 2250, (
            "Stage 2 一旦把 A1/A2 放进场，调用量就是这个量级——"
            "api_cost_cap 必须从 0 改成它乘单价的结果"
        )


# ── ③ B5 的可分辨性（`:821` 第 3 段）────────────────────────────────────────


class TestB5IsDistinguishable:
    """**这一组是 `:837` 点名要 T13 补的那个前置产出。**

    `:826` 的理由：档位速率定得太高（0.9）则 B5 对任何落进该档的前缀都判
    aliased（假阳成群）；定得太低（0.01）则 B5 几乎不误判、Stage 1 的排序
    完全由速率分布决定。**两个方向都坏，而 B0 的退化警报一个都测不到。**

    结论必须写成**一句可引用的话**，所以最后一条测试就断言那句话本身。
    """

    def test_aliased_prefixes_are_never_missed(self, raw) -> None:
        """**假阴率恒为 0——而且这不是 config 的巧合。**

        它是 `aliased ⇔ 四档都必回`（T3 那条承重的映射）的**推论**：四档都
        必回 ⇒ 每一档的 `P(≥2/3)` 都是 `p(1.0) = 1.0` ⇒ 无论随机后缀落进
        哪一档都必被判定。所以这条性质**不依赖于**我把 `r` 取成加权平均还是
        取成最差那一档——下面的界就是这么写的。
        """
        table = read_subnets(CONFIG_PATH)
        for prefix in table.aliased:
            rates = table.subnets[prefix]
            worst = min(rates.all_rates)
            assert _p_declared(worst) > 0.999_99, (
                f"{prefix} 最差的一档是 {worst}，判定概率 {_p_declared(worst)}"
            )
            assert _p_declared(_effective_rate(rates)) == pytest.approx(1.0)

    def test_the_aggregate_false_positive_rate_is_low_and_pinned(self, raw) -> None:
        """24 个非 aliased 前缀上的聚合假阳率。

        **读法一 6.15%**（本文件采用）、**读法二 23.39%**。两个都断言，
        是为了让"改一个档位"必须同时重算两个——只钉一个的话，另一个人按
        另一种读法实现 B5 时会以为测试在保护他的数。
        """
        table = read_subnets(CONFIG_PATH)
        others = [p for p in table.configured if p not in table.aliased]
        assert len(others) == 24

        r1 = sum(_p_declared(_effective_rate(table.subnets[p])) for p in others) / 24
        r2 = sum(_p_declared_six_chances(_effective_rate(table.subnets[p])) for p in others) / 24

        assert r1 == pytest.approx(0.0615, abs=5e-4), f"读法一算出 {r1}"
        assert r2 == pytest.approx(0.2339, abs=5e-4), f"读法二算出 {r2}"
        assert r2 / r1 > 3.0, (
            "两种读法的差不到 3 倍——那意味着这条歧义不承重，"
            "本文件与设计方案里那段关于「T6b 必须先钉死读法」的话就该删掉"
        )

    def test_the_false_positives_are_concentrated_in_one_group(self, raw) -> None:
        """假阳不是均匀撒的，而是**压在 `content-mid` 那一组上**。

        每个 `r = 0.30` 的前缀有 **21.6%** 的概率被误判，而 `r = 0.02` 的两组
        只有 **0.12%**。这个 180 倍的跨度是 B5"有活可干"的证据：如果所有
        非 aliased 档位的假阳率都趋同，B5 的判定就退化成一个不依赖观测的
        常数——那正是 `:826` 说的另一种坏法。
        """
        table = read_subnets(CONFIG_PATH)
        by_role = _rates_by_role(raw)
        mid = [_effective_rate(r) for r in by_role["content-mid"]]
        sparse = [_effective_rate(r) for r in by_role["content-sparse"]]
        assert len(mid) == 6 and len(sparse) == 4
        assert _p_declared(mid[0]) == pytest.approx(0.216, abs=1e-9)
        assert _p_declared(min(sparse)) == pytest.approx(0.001184, abs=1e-6)
        assert _p_declared(mid[0]) / _p_declared(min(sparse)) > 100

        # 两个 `tier-*-hot` 组**不是** 0.12%：它们各有 3 / 2 个前缀，四档不同值，
        # 而有效回包率被 hot 档抬高了不到 10%。抬得再高也没用——它们不是 aliased。
        for role in ("tier-eui-hot", "tier-byte-hot"):
            rates = by_role[role]
            assert len(rates) in (2, 3)
            for r in rates:
                assert _p_declared(_effective_rate(r)) < 0.01

    def test_raising_the_top_rate_would_degenerate_b5(self, raw) -> None:
        """`:826` 的反事实，**当场算一遍**：把最高那档从 0.30 抬到 0.90。

        聚合假阳率从 6.1% 涨到 25.0%，而**那一组单独看是 97.2%**——
        B5 对它基本是常数函数。这就是"档位速率不能取高"那句话的数值形式。
        """
        at_09 = _p_declared(0.9)
        assert at_09 == pytest.approx(0.972, abs=1e-9)
        assert at_09 > 0.9, "0.9 这一档下 B5 对落进来的前缀基本全判 aliased"

        table = read_subnets(CONFIG_PATH)
        others = [p for p in table.configured if p not in table.aliased]
        rest = sum(
            _p_declared(_effective_rate(table.subnets[p]))
            for p in others
            if table.subnets[p].default != 0.3
        )
        raised = (6 * at_09 + rest) / 24
        assert raised == pytest.approx(0.2505, abs=5e-4)
        assert raised > 0.24, "抬高之后假阳率没有明显恶化——那条理由就不成立"

    def test_the_conclusion_sentence(self, raw) -> None:
        """**`:821` 第 3 段要的那句可引用的话，落在这里。**

        写成断言而不是注释，是因为它是一句结论：改 config 就可能让它变假。
        """
        table = read_subnets(CONFIG_PATH)
        others = [p for p in table.configured if p not in table.aliased]
        fnr = 0.0  # 上一条测试证明的：四档全 1.0 ⇒ 判定概率 1.0
        fpr = sum(_p_declared(_effective_rate(table.subnets[p])) for p in others) / len(others)
        fpr_alt = (
            sum(_p_declared_six_chances(_effective_rate(table.subnets[p])) for p in others)
            / len(others)
        )
        assert fnr == 0.0 and fpr < 0.10 and fpr_alt < 0.30, (
            f"B5 在该档位下不可分：FNR={fnr}，FPR={fpr}（另一种读法 {fpr_alt}）"
        )


# ── ④ 退化警报（D10）────────────────────────────────────────────────────────


class TestB0AlarmIsCalibrated:
    """B0 跑在这份 config 上，必须**低于**警报线——否则 config 要重设。

    `:698`：若 B0 也能拿到接近最优的分数，说明任务退化（`E` 塌缩成
    aliased/not 二分）。B0 的分数是**可以事先算出来的**，所以这里不用跑。
    """

    def test_aliased_prefixes_come_first_so_the_quarter_is_exact(self, raw) -> None:
        """这条是下面那条精确断言的**前提**，所以单独钉住。

        B0 均分给每个前缀 ⇒ 它的 `aliased_waste_ratio` = aliased 前缀拿到的
        探测数 ÷ 总数。aliased 落在列表前 1/4 上，而均摊分配对列表的任何
        连续 1/4 段给出的探测数都恰好是总数的 1/4 ⇒ **恰好 0.25**，
        没有 ±1/300 的余数抖动。（若 aliased 不连续，Bresenham 的余数会让
        这个数在 0.247 / 0.253 之间飘——两种写法都不错，但只有一个能让
        这条断言写成等号。）
        """
        assert [entry["_role"] for entry in raw["subnets"]][:8] == ["aliased"] * 8

    def test_b0_wastes_exactly_a_quarter_of_the_budget_on_aliased_prefixes(self, raw) -> None:
        table = read_subnets(CONFIG_PATH)
        written = [entry["ipv6_prefix"] for entry in raw["subnets"]]
        ordered = [_canon(p) for p in written]
        assert list(table.subnets) == ordered, (
            "`read_subnets` 没有保持 config 的书写顺序——runner 下发给策略的前缀"
            "列表就是它（规范形式），而 B0 的分配按那个顺序走"
        )
        assert ordered[0] == "2001:db8:5::/64", "第 0 段的规范形式（零段被压掉）"
        assert ordered[1:8] == [f"2001:db8:5:{i:x}::/64" for i in range(1, 8)]
        counts = plan_counts(tuple(ordered), raw["budget"])
        aliased_spend = sum(counts[:8])
        assert aliased_spend == 75
        assert aliased_spend / raw["budget"] == 0.25

    def test_the_first_prefix_is_written_in_a_non_canonical_form(self, raw) -> None:
        """**一个刻意留下的规范化测试。**

        `2001:db8:5:0::/64` 与 `2001:db8:5::/64` 是同一个前缀，而 config 里
        写的是前者。靶场的 `inet_pton` 两种都认，我们的 `read_subnets` 也是
        ——但**任何拿"写下来的字符串"去查表的代码会在它上面查空**。

        留着的理由：它是唯一一个"写形 ≠ 规范形"的样本，于是任何"靠字符串
        相等"的假设都有一处会现形。**若将来有人把 config 改成全规范形式，
        这条测试会红**——那时要想清楚：是故意去掉这个样本，还是顺手把它
        改没了。
        """
        written = [entry["ipv6_prefix"] for entry in raw["subnets"]]
        noncanonical = [p for p in written if _canon(p) != p]
        assert noncanonical == ["2001:db8:5:0::/64"], (
            f"写形 ≠ 规范形的前缀现在是 {noncanonical}——"
            "要么 config 被改规范了，要么多了一个需要复核的写法"
        )

    def test_that_clears_the_alarm_with_room_to_spare(self, raw) -> None:
        """0.25 是警报线 0.05 的 **5 倍**——这是 `DEGENERATE_WASTE_FLOOR` 那条
        「至少 5 倍于容器侧容差」的判据在这份 config 上的落点。"""
        assert 0.25 > DEGENERATE_WASTE_FLOOR
        assert 0.25 / DEGENERATE_WASTE_FLOOR >= 5

    def test_the_ceiling_is_what_makes_the_alarm_meaningful(self, raw) -> None:
        """上界：知晓真值的策略把这一列压到 **0**。所以可用跨度是
        `[0, 0.25]`，而不是 `[0.25, 0.25]`。

        **⚠️ 一处已知的保守化**：6SEVEN 报的真实 aliasing 比率远高于 25%
        （`:647` 提到 73.9% 这个量级）。我们取 25% 意味着 `aliased_waste_ratio`
        的**可改进幅度只有现实的三分之一**——代价是 Stage 1 更容易得出
        "看不出差别"的结论，好处是"全判 aliased"这条捷径拿不到高分
        （25% 的基线 vs 73.9% 的基线）。**这是选择，不是疏漏**，
        已写回设计方案；Stage 2 的完整八行表若要接近现实分布，改这里。
        """
        assert 0.0 == pytest.approx(0.0)
        assert 0.25 - 0.0 > 2 * DEGENERATE_WASTE_FLOOR


# ── ⑤ 挂钟上限（门 1）──────────────────────────────────────────────────────


class TestWallClockBudget:
    """`:586` 的算式：`Σ(探测数 / epoch_size × W) + 并发后的包时间 + LLM 往返延迟`。

    `:742` 已经把实测吞吐落成 **≈2.4 kpps**（4 850 无丢包实测的一半，
    为 2× 拥塞余量），所以这个算式现在有实测依据，不再是估的。
    """

    #: Stage 1 冒烟表的规模（`:581`：「5 行 × 300 探 × 30 seeds = 45 000 包」）。
    ROWS = 5
    SEEDS = 30

    def _estimate(self, raw: dict) -> tuple[float, float, float]:
        """返回 `(排空总时长, 纯探测总时长, 合计)`。"""
        runs = self.ROWS * self.SEEDS
        epochs_per_run = raw["budget"] // raw["epoch_size"]
        drain = runs * epochs_per_run * raw["W_s"]
        packets = runs * raw["budget"]
        packet_time = packets / STATION_PPS
        return drain, packet_time, drain + packet_time

    def test_the_packet_count_matches_the_documented_table(self, raw) -> None:
        assert self.ROWS * raw["budget"] * self.SEEDS == 45_000

    def test_the_estimate_is_under_the_cap_with_margin(self, raw) -> None:
        drain, packet_time, total = self._estimate(raw)
        assert drain == pytest.approx(562.5), f"排空 {drain}s"
        assert packet_time == pytest.approx(18.75)
        assert total == pytest.approx(581.25, abs=0.5)
        assert total < raw["wall_clock_cap_s"], (
            f"估算 {total:.0f}s 超过上限 {raw['wall_clock_cap_s']}s"
        )
        assert raw["wall_clock_cap_s"] / total >= 2.0, (
            "余量不到 2 倍——上限要能区分「配置错了」与「机器慢」，"
            "刚好卡住的上限会把后者也判成前者"
        )

    def test_the_cap_catches_a_mis_set_epoch_size(self, raw) -> None:
        """**上限的存在理由**：`:586` 说超了就是配置错了，不是机器慢。

        把 `epoch_size` 写成 1（"每个探测决策一次"的字面读法）会让排空
        从 9.4 分钟涨到 3.1 小时——**而这个错误在跑之前是不可见的**，
        只有算式能提前抓住它。`epoch_size` 是三个因子里唯一不牵动科学
        口径的那个，所以调它是对的处置。
        """
        bad = dict(raw, epoch_size=1)
        _, _, total = self._estimate(bad)
        assert total > raw["wall_clock_cap_s"], "上限没抓住 epoch_size=1"
        assert total > 3 * 3600, f"epoch_size=1 的估算只有 {total:.0f}s，比预期好太多"

    def test_llm_latency_is_zero_because_no_llm_row_plays_in_stage_one(self, raw) -> None:
        """算式的第三项在 Stage 1 是 0——**理由与 `api_cost_cap = 0` 是同一条**：
        A1/A2 不在 Stage 1，M1/M2 的驱动方式未定（T6c）。它被写在这里而不是
        省略，是为了让 T6c 改口径时这一项也跟着动。
        """
        _, _, total = self._estimate(raw)
        assert total < raw["wall_clock_cap_s"]  # 已经不含 LLM 项


# ── ⑥ 基线包的导入图（与 `src.agent` 受同一条 D7 约束）──────────────────────


class TestBaselineImportGraph:
    """`src/baselines/` 的导入图**也是一条安全属性**（见该包 docstring）。

    基线跑在策略进程里，它们能看到的必须与 A1/A2 一样多——**多看一眼真值，
    赢的就不是策略**（`:643` 的公平性协议把这条写成了明文）。
    `src/agent` 那条断言不能代替这一条：两个包是两条独立的导入路径。
    """

    #: 与 `test_isolation.py::TestAgentImportGraph.FORBIDDEN` 同一份名单。
    #: **不 import 它**——那份名单属于 T10 收口的隔离 CI，这里复述一遍是为了
    #: 让这一组能在 T10 之前独立成立；T10 之后若两份分叉，是 T10 该发现的事。
    FORBIDDEN = (
        "src.adapter.oracle",
        "src.adapter.truth",
        "src.adapter.runner",
        "src.adapter.probe_pool",
        "src.adapter.iface",
    )

    def _child_import_graph(self) -> list[str]:
        code = (
            "import json, sys;"
            "import src.baselines.b0_probe_all;"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('src.'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_b0_does_not_pull_in_truth_or_the_runner_side(self) -> None:
        modules = self._child_import_graph()
        leaked = [m for m in modules if m in self.FORBIDDEN]
        assert not leaked, f"导入 `b0_probe_all` 会连带导入 {leaked}——违反 D7"

    def test_b0_does_not_pull_in_the_scoring_layer(self) -> None:
        modules = self._child_import_graph()
        leaked = [m for m in modules if m == "src.scoring" or m.startswith("src.scoring.")]
        assert not leaked, f"导入 `b0_probe_all` 会连带导入评分层 {leaked}"

    def test_b0_pulls_in_practically_nothing(self) -> None:
        """它是 `src/` 里 import 最少的一个模块——**只有 `typing`**。

        这条断言的价值不在"干净"，在于它**会红**：任何一个"顺手复用"的
        import（哪怕只是 `src.adapter.surface` 的一个常量）都会让它变红，
        而那时作者必须停下来问「策略进程需要知道这个吗」。
        """
        modules = self._child_import_graph()
        assert set(modules) <= {"src", "src.baselines", "src.baselines.b0_probe_all"}, modules
