"""决策缓存 —— **T5 在 Stage 1 的两样交付物之一**（`cache.py`）。

本文件钉住的是缓存键那条链上的 bug 1、3、4（设计方案 `:563`/`:565`/`:566`）：

| 断言 | 防的是 |
|---|---|
| `TestContentHashesAreInTheKey` | 改 prompt / 改工具池描述 / 改 config ⇒ **未命中**（bug 1、3） |
| `TestCrossProcessKeyStability` | 换个 `PYTHONHASHSEED` ⇒ **命中**（bug 4） |
| `TestFingerprintRefusesStaleCaches` | 键的算法改了 ⇒ 整份缓存**拒绝加载**，而不是"加载了但永不命中" |
| `TestSaveLoad` | 缓存随包发布（`:676`）要真的可读、可复现 |

> **判据 6(ii) 要求决策缓存命中率 == 100%**（`:739`）。这条判据只有在
> 「未命中是**可解释的**」时才是一句有意义的话——所以 `CacheStats` 与
> `CacheKey.differing()` 一起进这个文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.agent.cache import (
    CACHE_FORMAT,
    CacheFormatError,
    CacheKey,
    DecisionCache,
    KeyMaterialError,
    cache_fingerprint,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 一份**完整**的 config：六项 LLM 身份都在（设计方案 `:588`）。
#: 单独抽出来，是为了让"改了温度"这种测试写成一行 diff。
BASE_CONFIG = {
    "model": "claude-sonnet-5-5",
    "model_version": "2026-09-01",
    "temperature": 0.0,
    "top_p": 1.0,
    "max_tokens": 1024,
    "call_date": "2026-10-09",
}

BASE_TOOLS = {"probe": "向一个地址发探测", "report": "提交前缀的判定"}
BASE_STATE = {"epoch": 1, "remaining_budget": 900, "probes": {"2001:db8:dead::/64": 4}}


def make_key(
    *,
    state: object = None,
    seed: int = 7,
    prompt: str = "渲染后的 prompt 文本",
    tool_pool: dict | None = None,
    config: dict | None = None,
) -> CacheKey:
    return CacheKey.of(
        state=BASE_STATE if state is None else state,
        seed=seed,
        prompt=prompt,
        tool_pool=BASE_TOOLS if tool_pool is None else tool_pool,
        config=BASE_CONFIG if config is None else config,
    )


# ── bug 1 / 3：内容哈希在键里 ─────────────────────────────────────────────


class TestContentHashesAreInTheKey:
    """**T5 的 Verify 原话：单测断言「改 prompt 内容 ⇒ 缓存未命中」**（`:1403`）。

    bug 1 的失效方式很特别：改一句 prompt 之后重跑，**全量命中旧缓存**，
    于是结果不变，改动被误判为「无效」。**而 A1 与 A2 的唯一差别就是工具池，
    这个 bug 正落在 P1 的对照上。**
    """

    def test_prompt_change_changes_the_key(self) -> None:
        before = make_key(prompt="原始 prompt")
        after = make_key(prompt="原始 prompt。另外，请优先探左边。")
        assert before.hex != after.hex
        assert before.differing(after) == ("prompt",)

    def test_prompt_change_actually_misses_in_the_cache(self) -> None:
        """不只是"键不同"，而是**查一遍真的没中**。"""
        cache = DecisionCache()
        cache.store(make_key(prompt="旧的 prompt"), "{}")
        assert cache.lookup(make_key(prompt="新的 prompt")) is None
        assert cache.lookup(make_key(prompt="旧的 prompt")) == "{}"

    def test_whitespace_in_the_prompt_counts(self) -> None:
        """哈希的是**模型真正看到的那串字节**（`prompts/__init__.py`）。
        多一个换行也是模型看到的东西变了，所以它也必须让键变——
        "只改空白不算改"是一条会被想当然的约定，而约定会被忘。"""
        assert make_key(prompt="a\nb").hex != make_key(prompt="a b").hex

    def test_tool_description_change_changes_the_key(self) -> None:
        """**A2 的卖点是构造新原语**（`:618`）。同名但描述/契约变了，
        与多了一个工具对决策的影响是同一类的，所以两者都必须让键变。"""
        assert make_key(tool_pool={"probe": "向一个地址发探测"}).hex != make_key(
            tool_pool={"probe": "向一个地址发探测，可以指定 hop limit"}
        ).hex

    def test_adding_a_tool_changes_the_key(self) -> None:
        more = {**BASE_TOOLS, "sample": "在一个前缀里抽样 n 个地址"}
        assert make_key(tool_pool=more).hex != make_key().hex

    def test_tool_pool_order_does_not_change_the_key(self) -> None:
        """工具池的顺序由 `ToolPool` 排序决定，不该因为调用方先塞了哪个而变。"""
        reordered = {"report": BASE_TOOLS["report"], "probe": BASE_TOOLS["probe"]}
        assert make_key(tool_pool=reordered).hex == make_key().hex

    @pytest.mark.parametrize(
        "field, value",
        [
            ("model", "claude-opus-5-5"),
            ("model_version", "2026-10-01"),
            ("temperature", 0.1),
            ("top_p", 0.9),
            ("max_tokens", 2048),
            ("call_date", "2026-10-10"),
        ],
    )
    def test_every_llm_identity_field_is_in_the_key(self, field: str, value: object) -> None:
        """**六项身份逐个测**（`:588`），不是一个代表打发了。

        差 0.1 的温度就不是同一次实验了——这正是 `:588` 要求它进 config 的理由。
        用 parametrize 而不是一条 `!=`，是因为**漏掉某一项**是最可能的失效形式，
        而一条笼统的断言对漏项是瞎的。
        """
        changed = {**BASE_CONFIG, field: value}
        assert make_key(config=changed).hex != make_key().hex
        assert make_key(config=changed).differing(make_key()) == ("config",)

    def test_config_is_not_whitelisted(self) -> None:
        """刻意**不做字段白名单**（`cache.py`）：白名单的失效方式是
        「下一个人加了一个影响决策的字段，而没人记得回来改白名单」——
        那正是 bug 3 的形状。所以一个白名单外的字段也必须让键变。"""
        changed = {**BASE_CONFIG, "epoch_size": 25}
        assert make_key(config=changed).hex != make_key().hex

    def test_seed_is_in_the_key(self) -> None:
        assert make_key(seed=7).hex != make_key(seed=8).hex

    def test_state_is_in_the_key(self) -> None:
        other = {**BASE_STATE, "remaining_budget": 899}
        assert make_key(state=other).hex != make_key().hex

    def test_scheme_version_actually_participates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**`KEY_SCHEME` 必须真的在键里**，不是"写在文档里让后来者记得去删旧缓存"。

        这条用 monkeypatch 而不是读常量：读常量只能证明它**存在**，
        证不了它**参与计算**——而"版本号加了但没人读它"正是这一格最容易的失效。
        `cache.py` 通过模块全局读它（`str(KEY_SCHEME)`），所以 patch 模块属性有效。
        """
        import src.agent.cache as cache_module

        before = make_key()
        monkeypatch.setattr(cache_module, "KEY_SCHEME", cache_module.KEY_SCHEME + 1)
        after = make_key()

        assert before.hex != after.hex
        assert before.differing(after) == ("scheme",)


# ── 差异报告 ──────────────────────────────────────────────────────────────


class TestDiffering:
    """`:745` 要求决策序列的差异**可解释**。只报"未命中"是不够的。"""

    def test_identical_keys_report_nothing(self) -> None:
        assert make_key().differing(make_key()) == ()

    def test_two_changes_are_both_reported(self) -> None:
        changed = make_key(prompt="另一段 prompt", seed=99)
        assert set(make_key().differing(changed)) == {"prompt", "seed"}

    def test_report_is_in_a_fixed_order(self) -> None:
        """顺序固定，报告才能逐字比对（同 `save()` 按键排序的理由）。"""
        changed = make_key(config={**BASE_CONFIG, "temperature": 0.7}, prompt="x")
        assert make_key().differing(changed) == ("prompt", "config")


# ── 进键材料的形态 ────────────────────────────────────────────────────────


class TestKeyMaterialShape:
    """**`config` 不许传路径。** 这是会被人顺手写错的地方，所以在测试里也钉一条。

    传路径看起来"很规范"，但路径**在文件内容变化时不变**——换模型、改温度都会
    命中旧键。那正是「进 config 不等于进哈希」的坑。
    """

    @pytest.mark.parametrize("bad", ["agent/config.json", Path("agent/config.json")])
    def test_config_path_is_refused(self, bad: object) -> None:
        with pytest.raises(KeyMaterialError) as excinfo:
            make_key(config=bad)  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "路径" in message
        # 消息要说清**为什么**，否则下一个人只会把 str 包成 dict 再试一次。
        assert "内容" in message

    def test_tool_pool_path_is_refused(self) -> None:
        with pytest.raises(KeyMaterialError):
            make_key(tool_pool="tools.json")  # type: ignore[arg-type]

    def test_non_mapping_config_is_refused(self) -> None:
        with pytest.raises(KeyMaterialError):
            make_key(config=[("temperature", 0.1)])  # type: ignore[arg-type]

    def test_empty_prompt_is_refused(self) -> None:
        with pytest.raises(KeyMaterialError):
            make_key(prompt="")

    def test_bool_seed_is_refused(self) -> None:
        """`True` 是 `int` 的子类——收下它意味着 seed 那一格能出现一个
        看起来像数字的布尔。"""
        with pytest.raises(KeyMaterialError):
            make_key(seed=True)

    def test_unorderable_state_is_refused_loudly(self) -> None:
        """状态里有 `list` ⇒ `CanonicalError`。**必须炸**：静默的后果是
        缓存永久不命中（bug 2），而那看起来只是"这次没命中"。"""
        from src.agent.canonical import CanonicalError

        with pytest.raises(CanonicalError):
            make_key(state={"visited": ["a", "b"]})


# ── 查 / 存 ───────────────────────────────────────────────────────────────


class TestLookupAndStats:
    def test_miss_then_hit(self) -> None:
        cache = DecisionCache()
        key = make_key()
        assert cache.lookup(key) is None
        cache.store(key, "{}")
        assert cache.lookup(key) == "{}"

    def test_stats_count_both(self) -> None:
        cache = DecisionCache()
        cache.store(make_key(), "{}")
        cache.lookup(make_key())
        cache.lookup(make_key(seed=42))
        assert cache.stats.hits == 1
        assert cache.stats.misses == 1
        assert cache.stats.hit_rate == 0.5

    def test_hit_rate_is_nan_when_never_queried(self) -> None:
        """**不返回 0.0。** 一次都没查过与"查了全没中"是两件事，
        把它们折成同一个数正是这个模块反复在防的那类静默错误
        （同 `score.py` 拒绝零探测运行的理由）。"""
        rate = DecisionCache().stats.hit_rate
        assert rate != rate  # nan

    def test_a_perfect_replay_reaches_hit_rate_one(self) -> None:
        """判据 6(ii)（`:739`）的形状：**重放那一遍**命中率必须是 1。

        ⚠️ **分母是"重放那一遍"，不是"整个会话"。** 下面这个两遍的会话，
        累计命中率是 50%——如果谁把判据 6(ii) 读成"整个会话的累计命中率
        必须是 100%"，他会得到一条**永远不可能通过**的判据（第一遍跑的时候
        当然是全未命中）。所以这里两种口径都断言出来，把区别钉在测试里。

        真正的回放测试属 T9/T18；这里只是在缓存这一层把那个数算出来。
        """
        cache = DecisionCache()
        keys = [make_key(seed=s) for s in range(5)]

        for key in keys:  # 第一遍：跑一次（全未命中）
            cache.lookup(key)
            cache.store(key, "{}")
        after_first = cache.stats

        for key in keys:  # 第二遍：回放（必须全命中）
            cache.lookup(key)
        after_replay = cache.stats

        assert after_first.hits == 0
        assert after_first.misses == 5
        assert after_replay.hits - after_first.hits == 5
        assert after_replay.misses - after_first.misses == 0
        # 累计口径 ≠ 回放口径。两个都写出来，免得将来有人搞混。
        assert after_replay.hit_rate == 0.5

    def test_overwriting_a_key_is_allowed(self) -> None:
        cache = DecisionCache()
        key = make_key()
        cache.store(key, "一")
        cache.store(key, "二")
        assert cache.lookup(key) == "二"
        assert len(cache) == 1

    def test_only_text_is_stored(self) -> None:
        with pytest.raises(TypeError):
            DecisionCache().store(make_key(), {"action": "stop"})  # type: ignore[arg-type]


# ── 指纹：旧缓存必须被拒绝加载 ────────────────────────────────────────────


class TestFingerprintRefusesStaleCaches:
    """**改 `KEY_SCHEME` 让整份缓存作废——而这是"响的"作废。**

    换一种做法（照常加载）的表现是「命中率 0%」，与「这次真的没命中」完全同形。
    一个 0% 命中率且不报错的缓存，看起来就是"这次没命中"，而实际上整份文件
    已经没有意义了。
    """

    def test_round_trip_keeps_the_fingerprint(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        DecisionCache().save(target)
        header = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
        assert header == {"format": CACHE_FORMAT, "fingerprint": cache_fingerprint()}

    def test_wrong_fingerprint_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        target.write_text(
            json.dumps({"format": CACHE_FORMAT, "fingerprint": "deadbeef"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(CacheFormatError) as excinfo:
            DecisionCache.load(target)
        assert "指纹" in str(excinfo.value)

    def test_wrong_format_version_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        target.write_text(
            json.dumps({"format": CACHE_FORMAT + 1, "fingerprint": cache_fingerprint()}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(CacheFormatError):
            DecisionCache.load(target)

    def test_a_cache_without_a_header_is_refused(self, tmp_path: Path) -> None:
        """没有文件头的缓存 = 一份不知道自己是哪套键的缓存。拒绝，不猜。"""
        target = tmp_path / "cache.jsonl"
        target.write_text('{"key": "abc", "output": "{}"}\n', encoding="utf-8")
        with pytest.raises(CacheFormatError):
            DecisionCache.load(target)

    def test_an_empty_existing_file_is_refused(self, tmp_path: Path) -> None:
        """**存在的空文件 ≠ 不存在的文件。** 前者是被截断的缓存
        （写到一半断电），后者是"这次还没跑过"。"""
        target = tmp_path / "cache.jsonl"
        target.write_text("", encoding="utf-8")
        with pytest.raises(CacheFormatError):
            DecisionCache.load(target)

    def test_a_cache_written_under_another_scheme_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**这条才是「`KEY_SCHEME += 1` 到底管不管用」的答案。**

        场景就是真实发生的那一次：一份已经**随包发布**的缓存，被一份改了
        键算法的代码去读。它必须炸，因为唯一能说明"这份缓存整体失效"的信息
        就是方案版本——内容哈希对"键的算法变了"是完全瞎的。
        """
        import src.agent.cache as cache_module

        target = tmp_path / "cache.jsonl"
        published = DecisionCache()
        published.store(make_key(), '{"action": "stop", "reason": "x"}')
        published.save(target)

        monkeypatch.setattr(cache_module, "KEY_SCHEME", cache_module.KEY_SCHEME + 1)
        with pytest.raises(CacheFormatError) as excinfo:
            DecisionCache.load(target)
        assert "指纹" in str(excinfo.value)

    def test_a_missing_file_is_an_empty_cache(self, tmp_path: Path) -> None:
        assert len(DecisionCache.load(tmp_path / "nope.jsonl")) == 0

    def test_a_malformed_entry_line_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        target.write_text(
            json.dumps({"format": CACHE_FORMAT, "fingerprint": cache_fingerprint()})
            + "\n"
            + "{not json\n",
            encoding="utf-8",
        )
        with pytest.raises(CacheFormatError) as excinfo:
            DecisionCache.load(target)
        # 行号要报出来——否则一份几千行的缓存里找一个坏行是场噩梦。
        assert ":2" in str(excinfo.value)

    def test_an_entry_missing_its_output_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        target.write_text(
            json.dumps({"format": CACHE_FORMAT, "fingerprint": cache_fingerprint()})
            + "\n"
            + json.dumps({"key": "abc"})
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(CacheFormatError):
            DecisionCache.load(target)


# ── 落盘 / 读盘 ───────────────────────────────────────────────────────────


class TestSaveLoad:
    """`:676` 要求决策缓存**随包发布**。不发布，别人就复现不了 LLM 那两行；
    `:592` 把可复现性边界写成「以随包发布的缓存为界」。"""

    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "cache.jsonl"
        key = make_key()
        cache = DecisionCache()
        cache.store(key, '{"action": "stop", "reason": "探完了"}')
        cache.save(target)

        reloaded = DecisionCache.load(target)
        assert reloaded.lookup(key) == '{"action": "stop", "reason": "探完了"}'

    def test_saving_twice_is_byte_identical(self, tmp_path: Path) -> None:
        """**行序按键排序**，于是同一份缓存两次保存得到逐字节相同的文件
        ——`git diff` 里出现的是真的变化，不是顺序抖动。"""
        cache = DecisionCache()
        for seed in range(20):
            cache.store(make_key(seed=seed), f'{{"n": {seed}}}')
        first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        cache.save(first)
        cache.save(second)
        assert first.read_bytes() == second.read_bytes()

    def test_row_order_does_not_depend_on_insertion_order(self, tmp_path: Path) -> None:
        """**同一份内容、不同的写入顺序 ⇒ 逐字节相同的文件。**

        这条比"同一个对象存两次"强：后者插入顺序当然一样，所以**行不排序也照样
        通过**——而并发的两次运行往缓存里写的顺序本来就不同。发布出去的缓存要能
        在 `git diff` 里只显示真的变化。
        """
        keys = [make_key(seed=s) for s in range(20)]
        forward, backward = DecisionCache(), DecisionCache()
        for key in keys:
            forward.store(key, "x")
        for key in reversed(keys):
            backward.store(key, "x")
        one, other = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        forward.save(one)
        backward.save(other)
        assert one.read_bytes() == other.read_bytes()

    def test_no_temp_file_is_left_behind(self, tmp_path: Path) -> None:
        """先写临时文件再 `os.replace`——缓存要随包发布，半个文件比没有更坏
        （它看起来是一份完整的缓存）。而临时文件不能留在目录里当垃圾。"""
        target = tmp_path / "cache.jsonl"
        DecisionCache().save(target)
        assert [p.name for p in tmp_path.iterdir()] == ["cache.jsonl"]

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        target = tmp_path / "runs" / "stage1" / "cache.jsonl"
        DecisionCache().save(target)
        assert target.exists()

    def test_output_is_stored_verbatim(self, tmp_path: Path) -> None:
        """**只存原文。** 存解析结果的话，命中路径就绕过了 schema 校验
        （`decide.parse_action`）——一份被手改过的缓存就能把非法动作送进实验。"""
        raw = '  {"action": "stop", "reason": " 前后都有空白 "}  '
        cache = DecisionCache()
        cache.store(make_key(), raw)
        target = tmp_path / "cache.jsonl"
        cache.save(target)
        assert DecisionCache.load(target).lookup(make_key()) == raw


# ── bug 4：整套键跨进程稳定（自校验）──────────────────────────────────────


#: **字符串的 `frozenset` 要在里面**：只有字符串元素的集合迭代顺序依赖哈希
#: 种子（小整数不依赖）。所以它是这条测试对"集合不排序"这个变异的敏感源——
#: 换成整数集合，变异就测不出来了。理由详见 `test_canonical.py`。
_CHILD = (
    "import json, sys;"
    "from src.agent.cache import CacheKey;"
    "key = CacheKey.of(state={'hop_limits': frozenset({64, 255}), 'epoch': 2,"
    " 'probed': frozenset({'alpha', 'beta', 'gamma', 'delta', 'epsilon'})},"
    " seed=7, prompt='一段固定的 prompt',"
    " tool_pool={'probe': '探测', 'report': '报告'},"
    " config={'model': 'm', 'temperature': 0.0, 'nested': {'b': 1, 'a': 2}});"
    "print(json.dumps({'builtin': hash('probe'), 'hex': key.hex}))"
)


def _run_child(hash_seed: str) -> dict:
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


class TestCrossProcessKeyStability:
    """**T5(b) 的 Verify，也是 T17 的四条不可失败项之一**（设计方案 `:1352`）：
    「同一个键在两个不同的 `PYTHONHASHSEED` 值下必须相等」。

    这里测的是**整套键**（`test_canonical.py` 里那份测的是 `digest_of`）；
    多出来的一层是 `CacheKey.of` 自己——排序、拼接、逐分量摘要，任何一处
    用了内建 `hash()` 都会在这里红。

    为什么是自校验的（先证明加盐真的开着）见 `test_canonical.py` 的模块 docstring。
    """

    def test_builtin_hash_really_is_salted_in_these_children(self) -> None:
        assert _run_child("0")["builtin"] != _run_child("1")["builtin"], (
            "两个子进程的内建 hash 相同 —— `PYTHONHASHSEED` 没生效，"
            "下一条测试就是假的。常见原因是启动参数带了 `-I`（蕴含 `-E`）。"
        )

    def test_the_whole_key_is_identical_across_hash_seeds(self) -> None:
        assert _run_child("0")["hex"] == _run_child("1")["hex"]

    def test_the_key_is_identical_across_three_seeds(self) -> None:
        """多跑一个种子：两个种子的巧合（比如都落在同一侧）也挡掉。"""
        hexes = {_run_child(str(seed))["hex"] for seed in (0, 1, 12345)}
        assert len(hexes) == 1
