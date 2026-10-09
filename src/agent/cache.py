"""决策缓存 —— **T5 在 Stage 1 的两样交付物之一**（另一是 `state` 规范化）。

设计方案 `:557` 把键写成 `digest(canonical(state), seed, prompt, tool_pool, config)`，
`:561-566` 列出四个独立 bug。四条里有三条的修法落在本模块：

| # | bug | 修法 | 在哪儿 |
|---|---|---|---|
| 1 | 键里没有 prompt / 工具池版本（`:563`） | `prompt` 与 `tool_pool` 入键的是**内容摘要**，不是版本号 | `CacheKey.of()` |
| 3 | 键里没有 config 版本（`:565`） | `config` 整份入键（不是白名单几个字段） | 同上 |
| 4 | 内建 `hash()` 按进程加盐（`:566`） | 摘要只走 `canonical.digest()` | `canonical.py` |

（bug 2 是 `state` 到达顺序，落在 `canonical.py` 的类型规则上。）

## 三个刻意的"过度包含"

这一层的设计原则只有一条：**错的方向必须是"多未命中"（响），不能是"少未命中"（静默）。**
缓存多未命中只是贵一点、慢一点，看得见；少未命中会让**改动被误判为无效**，看不见。
三条都是这条原则的推论：

1. **`config` 整份入键，不做字段白名单。** 白名单的失效方式是：下一个人往 config 里
   加一个影响决策的字段，而没有人记得回来改白名单——**这正是 bug 3 的形状**。整份
   入键的代价是"改了个无关字段也会全量未命中"，而那是一条看得见、可解释的未命中。
2. **`prompt` 入的是渲染后的文本，不是模板名。** 只改 `render()` 而不改模板，模型看到
   的东西变了而键没变——又是 bug 1 的形状。
3. **`tool_pool` 入的是"名字 → 描述"的整份映射，不是名字的集合。** A2 的卖点是构造
   新原语（`:618`），而"同名但描述/契约变了"与"多了一个工具"对决策的影响是同类的。
   只收名字集合会让前者静默漏掉。

## `config` 不许传路径

**这是一个会被人顺手写错的地方，所以在这里拦死**：传 `"agent/config.json"` 这样的
路径看起来"很规范"，但路径**在文件内容变化时不变**——换模型、改温度都会命中旧键。
那正是我上一轮说的、「进 config 不等于进哈希」的坑。所以 `config` 必须是 `Mapping`，
传 `str` / `Path` 一律 `KeyMaterialError`。

## 缓存文件必须随包发布，所以它有格式版本

`:676` 要求决策缓存**随包发布**（不发布，别人就复现不了 LLM 那两行），`:592` 把可复现
边界写成「以随包发布的缓存为界」。一份要跨机器读的文件需要自己的格式版本，于是：

**文件头存一个 `cache_fingerprint`（键方案版本 + 文件格式版本）。指纹对不上时
`load()` 抛 `CacheFormatError`，而不是"加载进来然后永不命中"。** 理由与上面同一条：
一个 0% 命中率且不报错的缓存，看起来就是"这次没命中"，而实际上整份文件已经作废。
`KEY_SCHEME` 的维护规则见 `canonical.py`。
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import KEY_SCHEME, canonical, digest

#: 缓存**文件**格式版本。改字段名 / 增删行类型时 +1。
CACHE_FORMAT = 1

#: 键的五个分量，**顺序即这里写的顺序**（`components()` 与差异报告按它排）。
KEY_COMPONENTS = ("scheme", "state", "seed", "prompt", "tool_pool", "config")


class KeyMaterialError(ValueError):
    """进键的材料形态不对（例如 `config` 传了路径）。"""


class CacheFormatError(ValueError):
    """缓存文件与当前代码不是同一套键方案 / 文件格式。

    **它必须炸。** 一个指纹不匹配却照常加载的缓存，表现是"命中率 0%"——
    与"这次真的没命中"完全同形，而后者是常态。
    """


def cache_fingerprint() -> str:
    """当前代码的缓存指纹。**只反映键方案与文件格式，不反映实验内容。**

    这不矛盾：prompt、工具池、config 的变化**应该**只造成未命中（那是实验的一部分），
    只有键的**算法**变了才让整份缓存作废（那与实验无关，纯粹是记账口径变了）。
    """
    return digest(canonical({"format": CACHE_FORMAT, "scheme": KEY_SCHEME}))


@dataclass(frozen=True, slots=True)
class CacheKey:
    """一条决策缓存的键，外加**每个分量的摘要**（为了能解释"为什么没命中"）。

    `components` 存的是各分量的摘要而不是原值：`state` 里可能有整段观测，
    `prompt` 可能是几百字。存摘要既够用（比对相等/不等）又让缓存文件可读。
    """

    hex: str
    #: `(分量名, 该分量的摘要)`，按 `KEY_COMPONENTS` 排。**存在但不含原值。**
    components: tuple[tuple[str, str], ...]

    @classmethod
    def of(
        cls,
        *,
        state: Any,
        seed: int,
        prompt: str,
        tool_pool: Mapping[str, str],
        config: Mapping[str, Any],
    ) -> CacheKey:
        """算出键。`state` 走 `canonical()`，其余四个先各自摘要再入总材料。

        Args:
            state: 策略自生成的状态。**必须能被 `canonical()` 接受**（没有
                `list`、没有墙钟、没有非有限浮点）。
            seed: 这一轮的种子。
            prompt: **渲染后**的 prompt 文本（模型真正看到的字节）。
            tool_pool: `名字 → 描述`。整份入键，理由见模块 docstring。
            config: **整份**配置映射（含 LLM 身份六项，`:588`）。**不得传路径。**

        Raises:
            KeyMaterialError: `config` 是路径 / 不是映射，`tool_pool` 形态不对，
                或 `seed` 不是整数。
            CanonicalError: `state` 里有无法规范化的值。
        """
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise KeyMaterialError(f"seed 必须是整数，得到 {seed!r}")
        if not isinstance(prompt, str) or not prompt:
            raise KeyMaterialError("prompt 必须是渲染后的非空文本")
        _require_mapping(config, what="config")
        _require_mapping(tool_pool, what="tool_pool")

        component_values: dict[str, str] = {
            "scheme": str(KEY_SCHEME),
            "state": digest(canonical(state)),
            "seed": str(seed),
            "prompt": digest(canonical(prompt)),
            "tool_pool": digest(canonical(dict(tool_pool))),
            "config": digest(canonical(dict(config))),
        }
        material = canonical({name: component_values[name] for name in KEY_COMPONENTS})
        return cls(
            hex=digest(material),
            components=tuple((name, component_values[name]) for name in KEY_COMPONENTS),
        )

    def differing(self, other: CacheKey) -> tuple[str, ...]:
        """哪几个分量不同。**空元组 ⟺ 键相同。**

        这是设计方案 `:745`（V6-14 的两档口径）里「决策序列的差异**可解释**」那条
        要求的落点：改了 prompt 之后，"为什么这次没命中"必须能指到具体的分量，
        而不是只能说"未命中符合预期"。
        """
        mine = dict(self.components)
        theirs = dict(other.components)
        return tuple(name for name in KEY_COMPONENTS if mine.get(name) != theirs.get(name))


def _require_mapping(value: Any, *, what: str) -> None:
    if isinstance(value, (str, os.PathLike, bytes)):
        raise KeyMaterialError(
            f"{what} 收到了一条路径（{value!r}）。**这不是内容，是内容的位置**——"
            "文件内容变了而路径不变，于是换模型、改温度都会命中旧键（bug 3 的形状，"
            "设计方案 `:565`）。请传入读好之后的映射。"
        )
    if not isinstance(value, Mapping):
        raise KeyMaterialError(f"{what} 必须是映射，得到 {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CacheStats:
    """命中计数。**它是判据 6(ii) 里「决策缓存命中率 == 100%」那个量的载体**
    （设计方案 `:739`）。做成数据而不是日志，是为了让那条判据能被断言。"""

    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """总次数为 0 时返回 `float("nan")`。

        **不返回 0.0。** 一次都没查过与"查了全没中"是两件事，把它们折成同一个数
        正是本文件反复在防的那类静默错误（同 `score.py` 拒绝零探测运行的理由）。
        """
        if self.total == 0:
            return float("nan")
        return self.hits / self.total


class DecisionCache:
    """`键 → 模型的原始输出文本`。**只存原文，不存解析结果。**

    存**原始文本**而不是解析好的动作，是刻意的：于是**每次命中都要重新过一遍
    schema 校验**（`decide.parse_action`），校验器永远在关键路径上。若存解析结果，
    命中路径就绕过了校验——一份被手改过、或由更宽松的旧版本写入的缓存，就能把
    一条非法动作静默送进实验里。

    ## 调用方是 runner 侧，不是策略进程（⚠️ T10 的接口，见下）

    设计方案 `:1179`（数据流图第 ③ 条边）写着：**「策略进程不得读决策缓存文件
    本身——缓存里存着别的策略的决策，读到它等于拿到一份"别人怎么想的"的答案。」**
    同一张图 `:1154` 把决策缓存画在 **runner 进程的方框里**。

    **所以本类的能力与它的调用方不是一回事。** 本模块提供读写能力，但**谁允许调用
    它、经哪条路调用，是 T10 的沙箱规则**。当前代码里没有把缓存查询搬上 stdio 的
    接线——那要动 `protocol.CLIENT_VERBS`（一个允许清单，加动词必须同步
    `PROTOCOL_VERSION`），属 T2 的协议面，不在 T5 的 Files 里。**这条接口此前没有
    任何一处写出来过，是 T5 读码时发现的**，见 T5 的写回。
    """

    def __init__(self, entries: Mapping[str, str] | None = None) -> None:
        self._entries: dict[str, str] = dict(entries or {})
        self._hits = 0
        self._misses = 0

    # ── 查询 / 写入 ─────────────────────────────────────────────────────

    def lookup(self, key: CacheKey) -> str | None:
        """命中返回模型的原始输出文本；未命中返回 `None`。

        **返回 `None` 而不是抛异常**：未命中是常态（新 config、新 prompt、
        新 seed 都是全量未命中），不是错误。但**计数一定要动**——否则
        「命中率」这个量就无从观测，而判据 6(ii) 正靠它。
        """
        found = self._entries.get(key.hex)
        if found is None:
            self._misses += 1
            return None
        self._hits += 1
        return found

    def store(self, key: CacheKey, output: str) -> None:
        """记下模型的原始输出。**重写同一个键是允许的**（幂等）。"""
        if not isinstance(output, str):
            raise TypeError(f"缓存只存文本，得到 {type(output).__name__}")
        self._entries[key.hex] = output

    @property
    def stats(self) -> CacheStats:
        return CacheStats(hits=self._hits, misses=self._misses)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ── 落盘 / 读盘 ─────────────────────────────────────────────────────

    def save(self, path: str | os.PathLike[str]) -> None:
        """写成 JSONL。**先写临时文件再 `os.replace`**——缓存要随包发布，
        半个文件比没有文件更坏（它看起来是一份完整的缓存）。

        行序按键排序，于是**同一份缓存两次保存得到逐字节相同的文件**，
        `git diff` 里出现的是真的变化，不是顺序抖动。
        """
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {"format": CACHE_FORMAT, "fingerprint": cache_fingerprint()},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            )
            for key in sorted(self._entries):
                handle.write(
                    json.dumps(
                        {"key": key, "output": self._entries[key]},
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.replace(tmp, target)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> DecisionCache:
        """读一份缓存。**文件不存在 = 空缓存**（不是错误）；**指纹不符 = 抛**。

        Raises:
            CacheFormatError: 文件头缺失/不是 JSON/版本不符/指纹不符/条目行畸形。
        """
        target = pathlib.Path(path)
        if not target.exists():
            return cls()
        lines = target.read_text(encoding="utf-8").splitlines()
        if not lines or not lines[0].strip():
            raise CacheFormatError(f"{target} 是空的——它不该存在或者是被截断的")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise CacheFormatError(f"{target} 的第一行不是合法的 JSON 头") from exc
        if not isinstance(header, dict) or "format" not in header or "fingerprint" not in header:
            raise CacheFormatError(f"{target} 的文件头缺少 format/fingerprint")
        if header["format"] != CACHE_FORMAT:
            raise CacheFormatError(
                f"缓存文件格式是 {header['format']}，本代码是 {CACHE_FORMAT}。"
                "重新生成缓存，不要在旧文件上继续写。"
            )
        expected = cache_fingerprint()
        if header["fingerprint"] != expected:
            raise CacheFormatError(
                f"{target} 的键方案指纹是 {header['fingerprint']}，本代码是 {expected}。\n"
                "**这份缓存的键已经整体失去意义**（键的算法改过，见 canonical.KEY_SCHEME）。\n"
                "照常加载的表现是「命中率 0%」——与「这次真的没命中」完全同形，"
                "所以这里拒答而不是降级。"
            )
        entries: dict[str, str] = {}
        for lineno, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CacheFormatError(f"{target}:{lineno} 不是合法的 JSON") from exc
            if not isinstance(record, dict) or "key" not in record or "output" not in record:
                raise CacheFormatError(f"{target}:{lineno} 缺少 key/output")
            if not isinstance(record["output"], str):
                raise CacheFormatError(f"{target}:{lineno} 的 output 不是字符串")
            entries[record["key"]] = record["output"]
        return cls(entries)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"DecisionCache({len(self._entries)} 条, hits={self._hits}, misses={self._misses})"


__all__ = [
    "CACHE_FORMAT",
    "KEY_COMPONENTS",
    "CacheFormatError",
    "CacheKey",
    "CacheStats",
    "DecisionCache",
    "KeyMaterialError",
    "cache_fingerprint",
]
