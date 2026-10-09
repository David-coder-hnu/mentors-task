"""`canonical.py` —— 缓存键的地基（T5）。

这个文件要钉住的是**四条独立 bug 里的两条**（设计方案 `:561-566`）：

| bug | 本文件的断言 |
|---|---|
| 2. 并发使 `state` 依赖回包到达顺序（`:564`） | `TestOrderIndependence`、`TestListIsRejected` |
| 4. 内建 `hash()` 按进程加盐（`:566`） | `TestCrossProcessStability`（**自校验**，见下） |

> ## 为什么跨进程那条要「自校验」
>
> 一条「两个 `PYTHONHASHSEED` 下摘要相等」的测试有一个致命的假通过方式：
> **如果那个环境变量根本没生效，两个子进程其实共享同一个种子，测试就永远绿。**
> 而它绿的样子和真绿一模一样——这正是本仓库反复在防的那类静默失效。
>
> 所以子进程**同时报回两样东西**：`hash("probe")`（内建哈希，按进程加盐）
> 与我们的摘要。断言分两步：**先断言两个内建哈希确实不同**（证明加盐真的开着），
> **再断言两个摘要相同**（证明我们的键不受它影响）。第一步失败时测试是红的，
> 而不是假装通过。
>
> ⚠️ **子进程不能用 `-I` 启动**：`-I` 蕴含 `-E`，而 `-E` 会**忽略 `PYTHONHASHSEED`**
> ——于是加盐被关掉、第一步失效、整条测试变成一句空话。这类坑正是它要防的。
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.agent.canonical import (
    KEY_SCHEME,
    CanonicalError,
    canonical,
    digest,
    digest_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── ② 序无关 ──────────────────────────────────────────────────────────────


class TestOrderIndependence:
    """集合类容器的规范形式**不含顺序**。这就是 bug 2 的修法本身。"""

    def test_frozenset_is_order_independent(self) -> None:
        assert canonical(frozenset({1, 2, 3})) == canonical(frozenset({3, 1, 2}))

    def test_set_is_order_independent(self) -> None:
        assert canonical({1, 2, 3}) == canonical({3, 2, 1})

    def test_set_and_frozenset_share_a_form(self) -> None:
        """`set` 与 `frozenset` 装上同样的元素就是同一个值——
        否则 `Stage1State` 换个容器类型就会换一个键，而那是一次纯记账差异。"""
        assert canonical({1, 2}) == canonical(frozenset({1, 2}))

    def test_mapping_is_key_ordered(self) -> None:
        assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})

    def test_tuple_keeps_its_order(self) -> None:
        """元组的顺序**是**语义（动作序列、epoch 序列），所以它必须保序。
        这条与上面几条是一对：**哪些容器序有关、哪些无关，是显式决定的**，
        而不是"顺手 append 进去看看"。"""
        assert canonical((1, 2)) != canonical((2, 1))

    def test_a_batch_of_replies_has_one_form_per_multiset(self) -> None:
        """模拟一组回包：**到达顺序不同 ⇒ 同一个规范形式。**"""
        batch = [{"dst": f"2001:db8:dead::{i:x}", "status": "received"} for i in range(1, 8)]
        one = canonical(frozenset(tuple(sorted(item.items())) for item in batch))
        other = canonical(frozenset(tuple(sorted(item.items())) for item in reversed(batch)))
        assert one == other


# ── ② 的强制执行：`list` 被拒绝 ────────────────────────────────────────────


class TestListIsRejected:
    """**`list` 被拒绝是这个模块最承重的一条**（设计方案 `:564`）。

    一个 `list` 没有唯一的规范形式：顺序可能承载语义（那是 `tuple`），也可能
    不承载（那是 `frozenset`）。拒绝它，逼作者显式选一个。
    """

    def test_list_is_rejected(self) -> None:
        with pytest.raises(CanonicalError) as excinfo:
            canonical([1, 2, 3])
        message = str(excinfo.value)
        # 错误消息必须**给出出路**，否则下一个人只会把 list 换成 ... 再试一次。
        assert "tuple" in message
        assert "frozenset" in message

    def test_list_is_rejected_even_inside_a_mapping(self) -> None:
        """嵌套也不算例外——「在深一点的地方顺手写了个 list」是最常见的形式。"""
        with pytest.raises(CanonicalError):
            canonical({"replies": [1, 2]})

    def test_list_is_rejected_even_inside_a_tuple(self) -> None:
        with pytest.raises(CanonicalError):
            canonical((1, [2]))

    def test_the_rejection_message_names_the_arrival_order_bug(self) -> None:
        """消息要指到具体理由，而不是「不支持的类型」。"""
        with pytest.raises(CanonicalError) as excinfo:
            canonical([])
        assert "到达顺序" in str(excinfo.value) or "顺序" in str(excinfo.value)


# ── 墙钟与浮点 ────────────────────────────────────────────────────────────


class TestClockIsRejected:
    """`:564` 明写「时间类字段用逻辑序号而非墙钟」。**墙钟进键 ⇒ 键随时间变
    ⇒ 缓存永不命中，且不报错。**"""

    @pytest.mark.parametrize(
        "value",
        [
            datetime.datetime(2026, 10, 9, 12, 0, 0),
            datetime.date(2026, 10, 9),
            datetime.time(12, 0, 0),
            datetime.timedelta(seconds=1),
        ],
    )
    def test_time_types_are_rejected(self, value: object) -> None:
        with pytest.raises(CanonicalError):
            canonical(value)

    def test_the_rejection_message_names_the_logical_index(self) -> None:
        with pytest.raises(CanonicalError) as excinfo:
            canonical(datetime.datetime(2026, 10, 9))
        assert "逻辑序号" in str(excinfo.value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_are_rejected(self, value: float) -> None:
        """`nan != nan` —— 它进键之后「同一份 state」判不出相等。"""
        with pytest.raises(CanonicalError):
            canonical(value)

    def test_arbitrary_objects_are_rejected(self) -> None:
        """默认的 `repr()` 含内存地址 ⇒ 键每进程都不同。"""

        class Opaque:
            pass

        with pytest.raises(CanonicalError):
            canonical(Opaque())

    def test_non_string_mapping_keys_are_rejected(self) -> None:
        with pytest.raises(CanonicalError):
            canonical({1: "a"})


# ── 长度框架 ──────────────────────────────────────────────────────────────


class TestFraming:
    """拼接必须带长度前缀，否则**不同结构会撞成同一串字节**——而撞出来的
    缓存是"另一件事的答案被当成本次的答案"，是最难发现的一类错。"""

    def test_adjacent_strings_do_not_collide(self) -> None:
        """**这对值不是随便挑的。**

        长度前缀防的不是 `("ab","c")` vs `("a","bc")`——那一对**加不加前缀都不撞**
        （`sabsc` vs `sasbc`，标签字节 `s` 落在不同位置，自己就把它们分开了）。
        真正会撞的是**某个串的内容以标签字节开头**的形状：

        * `("as", "b")` ⇒ `t` + `2` + `sas` + `sb`
        * `("a", "sb")` ⇒ `t` + `2` + `sa` + `ssb`

        没有长度前缀时，两串完全一样（`t2sassb`）——于是**两个不同的 state 判成
        同一个键**，缓存会把"另一件事的答案"当作本次的答案返回。这是最难发现的
        一类错：它不报错，它给答案。
        """
        assert canonical(("as", "b")) != canonical(("a", "sb"))
        # 对照组：这一对即使没有长度前缀也不撞，所以它单独没有牙。
        assert canonical(("ab", "c")) != canonical(("a", "bc"))

    def test_empty_sequence_differs_from_a_single_none(self) -> None:
        assert canonical(()) != canonical((None,))

    def test_bool_differs_from_the_int_it_equals(self) -> None:
        """`True == 1` 在 Python 里成立，而在键里**必须**是两个值：
        它们的语义完全不同（"是 aliased" vs "探了 1 次"）。"""
        assert canonical(True) != canonical(1)

    def test_int_differs_from_the_float_it_equals(self) -> None:
        assert canonical(1) != canonical(1.0)

    def test_mapping_differs_from_a_sequence_of_its_pairs(self) -> None:
        assert canonical({"a": 1}) != canonical((("a", 1),))

    def test_repr_of_a_float_is_the_text_form(self) -> None:
        """`float` 走 `repr()`：同一台机器上稳定，且 `0.1` 与 `0.1000…01`
        不会因为格式化精度而塌成同一个值。"""
        assert canonical(0.1) != canonical(0.2)
        assert canonical(0.1) == canonical(0.1)


# ── ④ 跨进程稳定（自校验）─────────────────────────────────────────────────


#: 子进程算的那份值。**里面刻意放了一个字符串的 `frozenset`。**
#:
#: 这不是随便挑的：`frozenset` 的**迭代顺序依赖哈希种子**，而依赖种子的
#: 只有**字符串**元素（小整数的哈希就是它自己，顺序在各进程里都一样）。
#: 也就是说，如果 `canonical()` 的集合分支哪天不排序了，一个装着整数的
#: `frozenset` 照样能通过这条测试——**只有字符串能戳穿它**。
#: 这个值是这条测试有没有牙的关键，不是装饰。
_CHILD = (
    "import json, sys;"
    "from src.agent.canonical import digest_of, KEY_SCHEME;"
    "value = {'hop_limits': frozenset({64, 255}), 'epoch': 3,"
    " 'reported': {'a': True},"
    " 'probed': frozenset({'alpha', 'beta', 'gamma', 'delta', 'epsilon'})};"
    "print(json.dumps({'builtin': hash('probe'),"
    " 'digest': digest_of(value), 'scheme': KEY_SCHEME}))"
)


def _run_child(hash_seed: str) -> dict:
    """在一个**指定了 `PYTHONHASHSEED`** 的干净子进程里算一次摘要。

    `env` 里补 `PYTHONIOENCODING=utf-8`：错误消息是中文，而中文 Windows 上
    子进程默认走 cp936，报错时反而会因为编码问题把真正的错误盖掉。
    """
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestCrossProcessStability:
    """**T5(b) 的 Verify，也是 T17 的四条不可失败项之一**（设计方案 `:1352`）。

    Verify 的原话是：**「同一个键在两个不同的 `PYTHONHASHSEED` 值下必须相等」**。
    它要防的是 bug 4（`:566`）：内建 `hash()` 按进程加盐，于是一份用它生成的缓存
    在别人的机器上命中率为 0，**而它看起来是一份完整、合法、可发布的缓存**。
    """

    def test_builtin_hash_really_is_salted_in_these_children(self) -> None:
        """**先证明加盐开着。** 这条是上面那条的前提，不是多余的。

        如果 `PYTHONHASHSEED` 没生效（例如子进程被 `-I` 启动，或某个
        `sitecustomize` 把它吃掉了），两个子进程会共享同一个种子，
        下面那条「摘要相等」就变成一句空话——**而且它绿得毫无异常**。
        """
        first = _run_child("0")
        second = _run_child("1")
        assert first["builtin"] != second["builtin"], (
            "两个子进程的内建 hash 相同 —— `PYTHONHASHSEED` 没有生效。"
            "此时下一条测试是假的：它没有真的跨过种子。"
            "常见原因是启动参数里带了 `-I`（蕴含 `-E`，会忽略 PYTHONHASHSEED）。"
        )

    def test_digest_is_identical_across_hash_seeds(self) -> None:
        first = _run_child("0")
        second = _run_child("1")
        assert first["digest"] == second["digest"]

    def test_the_scheme_version_travels_with_the_digest(self) -> None:
        """`KEY_SCHEME` 是要被编进缓存文件指纹的（`cache.py`），
        所以它得是一个能被读到的模块级常量，而不是散落在各处的隐含假设。"""
        assert _run_child("0")["scheme"] == KEY_SCHEME


# ── `digest()` 是一个具名函数 ─────────────────────────────────────────────


class TestDigest:
    def test_digest_is_hex_sha256(self) -> None:
        """长度 64 的十六进制 —— 顺带把"有人把它换成内建 hash 再 `str()`"
        这种改法挡在门外（那个的结果是十进制数字，长度不定）。"""
        value = digest(b"probe")
        assert len(value) == 64
        assert all(ch in "0123456789abcdef" for ch in value)

    def test_digest_is_not_the_builtin_hash(self) -> None:
        assert digest(b"probe") != str(hash(b"probe"))

    def test_digest_of_matches_digest_of_canonical(self) -> None:
        assert digest_of({"a": 1}) == digest(canonical({"a": 1}))

    def test_different_bytes_give_different_digests(self) -> None:
        assert digest(b"a") != digest(b"b")
