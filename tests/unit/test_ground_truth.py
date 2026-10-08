"""`ground_truth` 的导出与承重映射 —— 纯 Python，不碰 Docker，**进 hosted CI**。

这组测试盯的是**真值本身**，所以它的失效方式很特殊：导错一个 `aliased`，
实验照跑、表照出、数字看起来正常——只是从第一个数开始就错了。因此这里
不测「函数被调用了」，只测**边界**：哪一档速率算「必回」、四档里少一档会
怎样、「沉默的前缀」与「不存在的前缀」能不能被分开。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.adapter.ground_truth import (
    ConfigError,
    SubnetRates,
    always_replies,
    read_subnets,
    stage1_truth_from_config,
)

#: 手工小 config —— 四类前缀各一个，**逐字段比对**用它（T3 的第一条 Verify）。
#: 每一条都在测一个具体的判断，不是随手凑数：
#:   aaaa = aliased 陷阱（四档全 1.0）
#:   bbbb = 真有内容，且**有一档是 1.0**（「三档为 1」绝不能被当成 aliased）
#:   cccc = 沉默（四档全 0）—— 与「不在 config 里」同形但不同义
#:   dddd = 四档字段全缺 —— 靶场 `subnet.value(k, 0.0)` 缺省为 0
SMALL_CONFIG: dict = {
    "_comment": "T3 的手工小 config。_comment 这类未知键靶场与我们都忽略。",
    "subnets": [
        {
            "ipv6_prefix": "2001:db8:aaaa::/64",
            "default_response_rate": 1.0,
            "EUI_response_rate": 1.0,
            "lower_response_rate": 1.0,
            "higher_response_rate": 1.0,
        },
        {
            "ipv6_prefix": "2001:db8:bbbb::/64",
            "default_response_rate": 0.05,
            "EUI_response_rate": 1.0,
            "lower_response_rate": 0.1,
            "higher_response_rate": 0.5,
        },
        {
            "ipv6_prefix": "2001:db8:cccc::/64",
            "default_response_rate": 0.0,
            "EUI_response_rate": 0.0,
            "lower_response_rate": 0.0,
            "higher_response_rate": 0.0,
        },
        {"ipv6_prefix": "2001:db8:dddd::/64"},
    ],
}

#: 一个 config 里**从未出现过**的前缀。它不被任何断言引用为「配置过」。
ABSENT_PREFIX = "2001:db8:beef::/64"

ALL_FOUR = ("default", "eui", "lower", "higher")


class TestFieldByField:
    """T3 的第一条 Verify：手工小 config 逐字段比对。"""

    def test_every_rate_is_read_verbatim(self) -> None:
        table = read_subnets(SMALL_CONFIG)
        rates = table.subnets["2001:db8:bbbb::/64"]
        # 顺序按 IPv6-trie.hpp:321-331 的判定顺序：EUI → lower → higher → default
        assert (rates.default, rates.eui, rates.lower, rates.higher) == (
            0.05,
            1.0,
            0.1,
            0.5,
        )

    def test_missing_rate_fields_default_to_zero(self) -> None:
        """靶场 `subnet.value("...", 0.0)`（`IPv6-trie.hpp:217-220`）。

        缺字段**不是错误**——它是「这一档的地址一个都不回」。把它当错误会
        让一份靶场跑得起来的 config 在真值这里被拒。
        """
        table = read_subnets(SMALL_CONFIG)
        assert table.subnets["2001:db8:dddd::/64"] == SubnetRates()

    def test_all_four_prefixes_are_read_and_nothing_else(self) -> None:
        assert read_subnets(SMALL_CONFIG).configured == {
            "2001:db8:aaaa::/64",
            "2001:db8:bbbb::/64",
            "2001:db8:cccc::/64",
            "2001:db8:dddd::/64",
        }

    def test_host_bits_are_masked_like_the_range(self) -> None:
        """`2001:db8::5/64` 与 `2001:db8::/64` 在靶场里是同一个节点。

        `parseIPv6` 存下全部 128 位，但匹配时只比较前 `prefix_len` 位
        （`IPv6-trie.hpp:290-295`），所以主机位写什么都不影响归属。
        """
        table = read_subnets({"subnets": [{"ipv6_prefix": "2001:db8::5/64"}]})
        assert list(table.subnets) == ["2001:db8::/64"]

    def test_truth_maps_each_prefix_to_its_aliasing_flag(self) -> None:
        truth = stage1_truth_from_config(SMALL_CONFIG)
        assert truth.prefixes == {
            "2001:db8:aaaa::/64": {"aliased": True},
            "2001:db8:bbbb::/64": {"aliased": False},
            "2001:db8:cccc::/64": {"aliased": False},
            "2001:db8:dddd::/64": {"aliased": False},
        }

    def test_only_the_all_ones_prefix_is_aliased(self) -> None:
        """`bbbb` 有一档是 1.0，仍**不是** aliased。

        aliased 的定义是「每一个**地址**都回包」（设计方案 `:135`），而
        `EUI_response_rate` 只覆盖 EUI-64 那一类地址。把「看起来回得很勤」
        读成 aliased，会让真值把一块正常的地址空间标成垃圾。
        """
        assert read_subnets(SMALL_CONFIG).aliased == {"2001:db8:aaaa::/64"}

    def test_a_path_is_accepted_like_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "small.json"
        path.write_text(json.dumps(SMALL_CONFIG), encoding="utf-8")
        assert read_subnets(path) == read_subnets(str(path))
        assert stage1_truth_from_config(path).prefixes == (
            stage1_truth_from_config(SMALL_CONFIG).prefixes
        )


class TestAliasingBoundary:
    """钉住**承重的那条映射**的边界。

    判据是 `hash(dst) % 1000 < rate * 1000`，不是 `rate == 1.0`
    （`punchingbag.cpp:147-157`）。这一组就是那句话的可执行版本。
    """

    def test_one_and_just_below_one_both_always_reply(self) -> None:
        assert always_replies(1.0) is True
        # 阈值 999.5 > 999，而 random_value 最大 999 → 恒真
        assert always_replies(0.9995) is True

    def test_exactly_point_nine_nine_nine_does_not(self) -> None:
        """阈值恰好 999.0，而 `random_value` 取得到 999 → 千分之一的地址不回。

        「几乎总是回」与「每一个都回」在真值里必须是两件事——aliased 的定义
        要的是后者。
        """
        assert always_replies(0.999) is False

    def test_ordinary_rates_obviously_do_not(self) -> None:
        assert always_replies(0.0) is False
        assert always_replies(0.5) is False

    def test_all_four_classes_at_one_is_aliased(self) -> None:
        assert SubnetRates(1.0, 1.0, 1.0, 1.0).is_frp is True

    @pytest.mark.parametrize("dropped", ALL_FOUR)
    def test_no_class_may_be_missing_from_the_all_ones(self, dropped: str) -> None:
        """四档**缺任何一档**都不算 aliased —— 那一档的地址不会回包。

        这条参数化就是「四档互斥且穷尽」这个前提的可执行版本：它逐一验证
        没有哪个类别是可以被漏掉的（漏掉一个，等价关系就断了）。
        """
        assert SubnetRates(1.0, 1.0, 1.0, 1.0).is_frp is True
        assert replace(SubnetRates(1.0, 1.0, 1.0, 1.0), **{dropped: 0.999}).is_frp is False


class TestTwoSets:
    """T3 的第二条 Verify：**「未配置前缀」与「rate==0 前缀」是两个不同集合**。

    这对东西在观测上同形（都不回包），所以真值里一旦把它们合并，
    **没有任何运行期症状**会提示你——这正是失败模式表 `:1174` 点的那一条。
    """

    SILENT = "2001:db8:cccc::/64"

    def test_the_silent_prefix_is_configured_and_the_absent_one_is_not(self) -> None:
        table = read_subnets(SMALL_CONFIG)
        assert self.SILENT in table.configured
        assert ABSENT_PREFIX not in table.configured

    def test_the_two_are_indistinguishable_by_observation(self) -> None:
        """两者都不会回包——所以**只靠探测**永远分不开它们。

        这一条是上一条的对照：它说明为什么这个区分只能由真值导出提供，
        不能指望某个下游从观测里恢复出来。
        """
        table = read_subnets(SMALL_CONFIG)
        assert table.subnets[self.SILENT].can_reply is False
        assert ABSENT_PREFIX not in table.subnets  # 连一条速率记录都没有

    def test_configured_and_responsive_are_different_sets(self) -> None:
        """两个集合的差集**恰好**是沉默的那些前缀。"""
        table = read_subnets(SMALL_CONFIG)
        assert table.configured != table.responsive
        assert table.configured - table.responsive == {
            "2001:db8:cccc::/64",
            "2001:db8:dddd::/64",
        }
        assert table.silent == table.configured - table.responsive
        assert table.aliased <= table.responsive, "沉默的前缀不可能是 aliased"

    def test_the_truth_keeps_silent_prefixes(self) -> None:
        """沉默 ≠ 不存在：它必须留在 `prefixes` 里。

        删掉它，落在它上面的探测就**无法归属**——而「无法归属」在下游会
        静默退化成「不是 aliased」，正是这条失败模式本身。
        """
        truth = stage1_truth_from_config(SMALL_CONFIG)
        assert self.SILENT in truth.prefixes
        assert truth.prefixes[self.SILENT]["aliased"] is False
        assert ABSENT_PREFIX not in truth.prefixes

    def test_the_table_carries_what_the_truth_cannot(self) -> None:
        """`Stage1Truth` **回答不了**「在不在 config 里」——设计如此。

        所以那句断言必须落在 `SubnetTable` 上。这条测试把两者的分工写死：
        真值回答 aliasing，表回答归属；想用一个去替另一个，就会掉进
        `dict.get(p, {"aliased": False})` 那个坑（下面三行就是那个坑）。
        """
        truth = stage1_truth_from_config(SMALL_CONFIG)
        hazard = [truth.prefixes.get(p, {"aliased": False})["aliased"] for p in (
            self.SILENT,
            ABSENT_PREFIX,
        )]
        assert hazard == [False, False], "get 的默认值把『不存在』读成了『不是 aliased』"

        table = read_subnets(SMALL_CONFIG)
        assert (self.SILENT in table.configured) is True
        assert (ABSENT_PREFIX in table.configured) is False


class TestRangeParity:
    """我们与靶场对**同一份 config** 的理解必须一致，不一致的地方必须拒绝。

    靶场读 config 的方式有三处是「静默容忍」的（缺前缀跳过、解析失败跳过、
    重复前缀后者胜）。照抄它的容忍，然后**在我们这一侧把它们变成响的**——
    因为靶场那边一条防线都没有。
    """

    def test_entry_without_a_prefix_is_skipped_like_the_range(self) -> None:
        table = read_subnets({"subnets": [{"default_response_rate": 0.5}]})
        assert table.subnets == {}
        assert len(table.skipped) == 1
        assert "ipv6_prefix" in table.skipped[0]

    def test_unparseable_prefix_is_skipped_like_the_range(self) -> None:
        table = read_subnets({"subnets": [{"ipv6_prefix": "2001:zz::/64"}]})
        assert table.subnets == {}
        assert "解析失败" in table.skipped[0]

    def test_a_prefix_without_a_slash_is_skipped_like_the_range(self) -> None:
        """**这条差点被漏掉，因为 Python 与 C++ 在这里不一致。**

        靶场 `parseIPv6` 第一句就找 `/`，找不到直接抛（`IPv6-trie.hpp:60-61`），
        整条 subnet 被 catch 掉。而 `ipaddress.ip_network("2001:db8::")`
        **不报错**——它返回 `2001:db8::/128`。照直用 `ipaddress` 就会凭空
        多出一条 `/128` 主机路由，而且它在观测上从不回包，于是静默混进
        「沉默的前缀」那一类——正是 `TestTwoSets` 在防的那团东西。
        """
        table = read_subnets({"subnets": [{"ipv6_prefix": "2001:db8::"}]})
        assert table.subnets == {}
        assert "缺 '/'" in table.skipped[0]

    def test_the_truth_refuses_to_drop_a_subnet_silently(self) -> None:
        """靶场跳过一条前缀时**不报错、不留痕**，所以这里必须炸。

        忍下来的后果不是「少一个前缀」那么轻：它会直接改
        `aliased_waste_ratio` 的分母，而分母变了没有任何东西会提示。
        """
        with pytest.raises(ConfigError, match="被靶场跳过"):
            stage1_truth_from_config({"subnets": [{"ipv6_prefix": "2001:db8::"}]})

    def test_duplicate_prefix_last_one_wins_like_the_range(self) -> None:
        """靶场 `insert` 复用已有节点、`setRates` 覆盖（`IPv6-trie.hpp:215-221`）。"""
        table = read_subnets(
            {
                "subnets": [
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.1},
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.9},
                ]
            }
        )
        assert table.subnets["2001:db8::/64"].default == 0.9

    def test_a_duplicate_with_identical_rates_is_not_a_conflict(self) -> None:
        table = read_subnets(
            {
                "subnets": [
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.3},
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.3},
                ]
            }
        )
        assert table.conflicting == ()
        assert stage1_truth_from_config(
            {
                "subnets": [
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.3},
                    {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.3},
                ]
            }
        ).prefixes == {"2001:db8::/64": {"aliased": False}}

    def test_conflicting_duplicate_stops_the_truth(self) -> None:
        """写了两条不同速率的同名前缀，人多半以为两条都生效了。"""
        with pytest.raises(ConfigError, match="后者胜"):
            stage1_truth_from_config(
                {
                    "subnets": [
                        {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 0.1},
                        {"ipv6_prefix": "2001:db8::/64", "default_response_rate": 1.0},
                    ]
                }
            )

    @pytest.mark.parametrize("document", [{}, {"subnets": {}}, {"subnets": "x"}])
    def test_a_missing_subnets_array_is_rejected(self, document: dict) -> None:
        """靶场原话：`JSON does not contain a valid 'subnets' array`（`:205-206`）。

        这里与靶场**抛得一样早**是对的——差别在于它的失败方式是进程起不来，
        而我们要的是**在跑到一半之前**就停。
        """
        with pytest.raises(ConfigError, match="subnets"):
            read_subnets(document)

    @pytest.mark.parametrize("rate", [1.5, -0.1])
    def test_an_out_of_range_rate_is_rejected_not_skipped(self, rate: float) -> None:
        """**这一条不能照抄靶场的容忍。**

        靶场照收 1.5（阈值 1500，于是恒回），也照收 -0.1（恒不回）。若我们
        把它当「解析失败」跳掉，就等于**悄悄丢了一个靶场会照做的前缀**——
        那正是 `test_the_truth_refuses_to_drop_a_subnet_silently` 要防的分叉，
        只是从另一边进来。
        """
        with pytest.raises(ConfigError, match="越界"):
            read_subnets(
                {
                    "subnets": [
                        {
                            "ipv6_prefix": "2001:db8::/64",
                            "default_response_rate": rate,
                        }
                    ]
                }
            )

    @pytest.mark.parametrize("bad", [True, "0.5", None])
    def test_a_non_numeric_rate_is_skipped_like_the_range(self, bad: object) -> None:
        """nlohmann 的 `value<double>` 收不下 bool / 字符串 → 靶场整条 catch 掉。

        `True` 单列出来是因为 Python 里 `isinstance(True, int)` 为真——
        不显式排掉它，`true` 会被读成速率 1.0，于是一个笔误变成 aliased 前缀。
        """
        table = read_subnets(
            {"subnets": [{"ipv6_prefix": "2001:db8::/64", "default_response_rate": bad}]}
        )
        assert table.subnets == {}
        assert "不是数" in table.skipped[0]

    def test_a_non_string_prefix_stops_us_where_it_stops_the_range(self) -> None:
        """非字符串前缀会让靶场**起不来**（那行在 `try` 外面，`:212`）。

        所以这里也拒——「靶场跳过」与「靶场崩掉」是两种态度，不能混成一种。
        """
        with pytest.raises(ConfigError, match="不是字符串"):
            read_subnets({"subnets": [{"ipv6_prefix": 12345}]})

    def test_an_unknown_top_level_key_is_ignored(self) -> None:
        """`_comment` 这类键必须被忽略 —— 靶场只找 `subnets`（`:205`）。

        这条同时覆盖上面那份 `SMALL_CONFIG` 自己带的 `_comment`。
        """
        table = read_subnets(
            {"_comment": "随便写", "subnets": [{"ipv6_prefix": "2001:db8::/64"}]}
        )
        assert table.configured == {"2001:db8::/64"}

    def test_a_missing_file_is_a_config_error_not_a_crash(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="不存在"):
            read_subnets(tmp_path / "nope.json")

    def test_malformed_json_is_a_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="合法 JSON"):
            read_subnets(path)

    def test_a_wrong_source_type_is_a_type_error(self) -> None:
        with pytest.raises(TypeError, match="路径或 Mapping"):
            read_subnets(42)  # type: ignore[arg-type]


class TestSmokeConfig:
    """拿**靶场自带的**六份 config 里那份 `::/0` 形状来对一遍。

    用 `::/0` 是有价值的边界：它让「不在 config 里」成为**空集**
    （每个地址都落进去），于是 `TestTwoSets` 的那套断言在这里**测不到东西**。
    两件事都要钉：形状读得出来，以及这个 config 不适合测「两个集合」。
    """

    CONFIG = {
        "subnets": [
            {
                "ipv6_prefix": "::/0",
                "default_response_rate": 1.0,
                "EUI_response_rate": 1.0,
                "lower_response_rate": 1.0,
                "higher_response_rate": 1.0,
            }
        ]
    }

    def test_the_catch_all_prefix_reads_as_aliased(self) -> None:
        """四档全 1.0 的 `::/0` 就是「全网都回包」——按定义正是 aliased。

        这条也顺带钉住 `::/0` 的规范化：`ipaddress` 输出 `::/0`，与 config
        写的一致，所以下游按字符串查得到。
        """
        table = read_subnets(self.CONFIG)
        assert table.configured == {"::/0"}
        assert table.aliased == {"::/0"}
        assert stage1_truth_from_config(self.CONFIG).prefixes == {"::/0": {"aliased": True}}

    def test_a_catch_all_leaves_no_room_for_an_unconfigured_prefix(self) -> None:
        table = read_subnets(self.CONFIG)
        assert table.configured == table.responsive == table.aliased
        # 任何地址都落在 `::/0` 里，所以「未配置」这一格在语义上是空的：
        # 想测「沉默 vs 不存在」，config 必须用**分段**前缀，不能用 `::/0`。
        assert ABSENT_PREFIX not in table.configured, (
            "这条断言只是说明「::/0 之下没有谁是真的不在 config 里」——"
            "它成立是因为我们只查了一个具体字符串，不是因为集合为空"
        )
