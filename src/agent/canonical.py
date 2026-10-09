"""`state` 的规范化与缓存键摘要 —— **T5 的三条 critical gap 里有两条住在这里**。

设计方案把决策缓存键写成 `digest(canonical(state), seed, prompt, tool_pool, config)`
（`:557`），并列出四个独立的 bug（`:561-566`）。其中两条的修法落在本模块：

| bug | 位置 | 本模块的对策 |
|---|---|---|
| 2. 并发使 `state` 依赖回包到达顺序（`:564`） | 集合类字段排序、任何与到达顺序相关的量必须显式排序或聚合后再入 `state` | `canonical()` 的**类型规则**，见下 |
| 4. 内建 `hash()` 按进程加盐（`:566`） | 同一份 `(config, seed)` 在两个进程里算出不同的键 ⇒ **缓存永不命中，且静默** | `digest()` 只用 `hashlib.sha256`；**全文禁止内建 `hash()` 参与任何缓存键** |

## 为什么 `canonical()` 是一个**有类型规则**的序列化器

它的目标不是"能把对象变成字节"，而是**让『顺序有没有语义』这件事无法被遗忘**。
所以规则是白名单，且**逐类显式**：

| 输入 | 处理 | 理由 |
|---|---|---|
| `None` / `bool` / `int` / `str` / `bytes` | 带标签 + 长度前缀 | 长度前缀让 `("ab", "c")` 与 `("a", "bc")` 不撞 |
| `float` | `repr()`；**非有限值一律拒绝** | `nan != nan`，而 `nan` 混进键里会让「同一份 state」判不出相等 |
| `tuple` | **保序** | 元组的顺序通常是语义（动作序列、epoch 序列） |
| `set` / `frozenset` | **按各自规范字节排序** | 集合按定义没有顺序，排序是唯一确定的选择 |
| `Mapping` | 按键的规范字节排序；**键必须是 `str`** | JSON 的键是字符串；混入非串键后排序跨类型不可比 |
| `list` | **拒绝** | 见下 |
| `datetime` / `date` / `time` | **拒绝**（单独给一条消息） | 墙钟入 `state` ⇒ 键随时间变 ⇒ 缓存永不命中（`:564` 明写"时间类字段用逻辑序号而非墙钟"） |
| 其它对象 | **拒绝** | 默认 `repr()` 含内存地址 ⇒ 键每进程都不同 |

**`list` 被拒绝是刻意的，也是这个模块最承重的一条。** 一个 `list` 没有唯一的规范
形式：顺序可能承载语义（那是 `tuple`），也可能不承载（那是 `frozenset`）。让作者
**显式选一个**，正是 bug 2 的修法——并发池下回包到达顺序每次都不同，一个"想都没想
就 append 进去"的 `list` 会把这个顺序悄悄灌进缓存键，于是**缓存永不命中，而报错是
静默的**（看起来只是"这次没命中"）。拒绝 `list` 把这件事从静默变成当场炸。

> **这一条不是洁癖，它是这个模块存在的理由。** 设计方案 `:1237` 那句「最危险的
> 错误不是崩溃，是**看起来跑通了**」在这里有最直接的落点：一个靠 `list` 顺序的
> 缓存键不会抛任何异常，它只会让每一次决策都重新问一遍 LLM，而结果**看起来完全
> 正常**——只是不可复现。

## `KEY_SCHEME` 是这里唯一需要人维护的版本号

设计方案 `:1090` 要求「状态规范化写进规格并**清掉残留旧键**」。`prompt` 与
`tool_pool` 不靠版本号（它们靠**内容哈希**，见 `cache.py`），但**键的算法本身**
改了之后，旧的键就整体失去意义，没有任何内容哈希能反映这件事。所以：

**改动本模块的类型规则、标签、排序键或摘要算法时，必须同时 `KEY_SCHEME += 1`。**
`cache.py` 把它编进缓存文件的指纹（`cache_fingerprint`），于是旧格式的缓存是
**拒绝加载**，而不是"加载了但永不命中"——又是一个"静默"被换成"响的"。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import math
from collections.abc import Mapping
from typing import Any

#: 缓存键算法的版本。**改动本模块任何影响输出的规则时必须 +1**，理由见模块 docstring。
KEY_SCHEME = 1

#: 摘要算法。**不是内建 `hash()`** —— 后者在 PEP 456 下按进程加盐（`:566`）。
#: 换成 `blake2b` 也要同时 `KEY_SCHEME += 1`（摘要变了，旧键失去意义）。
_DIGEST = "sha256"


class CanonicalError(ValueError):
    """一个值没有规范形式。

    **它必须炸，不能退回 `repr()`。** 退回的后果不是"这次键怪怪的"，是
    **缓存永不命中且不报错**——`repr()` 对多数对象含内存地址，于是键每进程
    都不同，而整条链路上没有任何一处会红。
    """


def _frame(payload: bytes) -> bytes:
    """长度前缀。**顺序拼接不加分隔必然出事**：`("ab","c")` 与 `("a","bc")`
    会拼出同一串字节，而它们是两个不同的 state。"""
    return f"{len(payload)}:".encode("ascii") + payload


def canonical(value: Any) -> bytes:
    """把一个值序列化成**唯一的**字节串。

    同一个值在任何进程、任何 `PYTHONHASHSEED`、任何机器上都必须得到同一串字节。
    这是缓存键全部可复现性的地基。

    Raises:
        CanonicalError: 值落在白名单外，或含非有限浮点数、非字符串键、
            非规范类型（`list` / `datetime` / 任意对象）。
    """
    if value is None:
        return b"n"
    if value is True:
        return b"b1"
    if value is False:
        return b"b0"
    # 顺序要紧：bool 是 int 的子类，上面两条必须先拦掉。
    if isinstance(value, int):
        return _frame(b"i" + str(value).encode("ascii"))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError(
                f"state 里出现非有限浮点数 {value!r}。`nan != nan`，而它进键之后"
                "「同一份 state」判不出相等 —— 缓存键会永久漂移且不报错。"
            )
        return _frame(b"f" + repr(value).encode("ascii"))
    if isinstance(value, str):
        return _frame(b"s" + value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _frame(b"y" + bytes(value))

    if isinstance(value, tuple):
        # **保序**：元组的顺序通常是语义（动作序列、已走过的前缀序列）。
        return b"t" + _items(value)

    if isinstance(value, (set, frozenset)):
        # **按各自规范字节排序**：集合按定义没有顺序。排序是唯一确定的选择，
        # 也是 bug 2 的修法本身。
        return b"F" + _items(sorted(value, key=canonical))

    if isinstance(value, Mapping):
        parts: list[bytes] = []
        for key in value:
            if not isinstance(key, str):
                raise CanonicalError(
                    f"映射的键必须是字符串，得到 {type(key).__name__} ({key!r})。"
                    "非串键之间没有跨类型的全序，排序结果会随实现变化。"
                )
        for key in sorted(value):
            parts.append(_frame(canonical(key) + canonical(value[key])))
        return b"m" + _items_from(parts)

    if isinstance(value, (list,)):
        raise CanonicalError(
            "`list` 没有唯一的规范形式，所以这里拒绝它。**请显式选一个**：\n"
            "  · 顺序**是**语义（动作序列、epoch 序列）→ 换 `tuple`\n"
            "  · 顺序**不是**语义（一批回包、一组已探前缀）→ 换 `frozenset`\n"
            "这条拒绝就是 bug 2（并发下到达顺序污染缓存键，设计方案 `:564`）的修法："
            "并发池下回包顺序每次都不同，一个顺手的 `list` 会把它悄悄灌进键里，"
            "于是缓存永不命中，而症状只是「这次没命中」——不报错。"
        )

    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time, _datetime.timedelta)):
        raise CanonicalError(
            f"state 里出现了时间类值 {type(value).__name__}。墙钟入 state ⇒ 键随时间变 "
            "⇒ 缓存永不命中（设计方案 `:564`：时间类字段必须用**逻辑序号**，"
            "例如 epoch 编号、第 n 次探测，而不是墙钟）。"
        )

    raise CanonicalError(
        f"{type(value).__name__} 没有规范形式，拒绝入 state。默认的 `repr()` 含内存地址，"
        "会让缓存键每个进程都不同。若确实需要它，请把它拆成基本类型，"
        "或在 `canonical()` 里加一条带标签的规则并 `KEY_SCHEME += 1`。"
    )


def _items(values: Any) -> bytes:
    """`count` 前缀 + 逐项的（长度框架）拼接。count 让 `()` 与 `(None,)` 不撞。"""
    parts = [canonical(item) for item in values]
    return _items_from(parts)


def _items_from(parts: list[bytes]) -> bytes:
    return _frame(str(len(parts)).encode("ascii") + b"".join(_frame(p) for p in parts))


def digest(payload: bytes) -> str:
    """字节串 → 十六进制摘要。**内建 `hash()` 在这里没有代码路径。**

    这是 bug 4（`:566`）的修法，也是它为什么必须是一个具名函数而不是
    行内 `hashlib.sha256(...)`：**让"用稳定摘要"成为唯一顺手的选择**，
    而"用内建 hash"需要先删掉这个函数。
    """
    return hashlib.new(_DIGEST, payload).hexdigest()


def digest_of(value: Any) -> str:
    """任意可规范化的值 → 十六进制摘要。`canonical()` 的便捷封装。"""
    return digest(canonical(value))


__all__ = ["KEY_SCHEME", "CanonicalError", "canonical", "digest", "digest_of"]
