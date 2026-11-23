"""`ProbeSurface` —— **交给被测策略的唯一对象**（D7 / 设计方案 `:373`）。

它跑在**策略进程**里，是一个跨进程代理：每个方法变成一行 stdio 请求，回包被
解成 `Response`。真正的探测在 runner 进程（那里才有 socket 与真值）。

## 这个模块的导入图**就是**一条安全属性

它只导入 `protocol` 与 `types`。**不得**导入 `truth` / `oracle` / `runner` /
`probe_pool` / `iface`——前两个直接持有真值，后三个持有 socket 与靶场。

这条不是风格建议：`tests/unit/test_isolation.py::TestImportGraph` 会把
`src.adapter.surface` 在一个干净子进程里导入，断言 `sys.modules` 里没有
`src.adapter.truth`。**新加一条通往真值的 import 会让那条测试变红**——这是
v5 删掉「类型边界 + `hasattr` 断言」之后，隔离唯一还能被自动验证的地方。
`hasattr` 断言防不住会写代码的 A2（`:389`），导入图能。

## `reset()` 是纯本地的

设计方案 `:314`：`reset()` **只重置策略侧**（记忆、预算视图）。靶场的重置是
`Oracle.reset_target(seed)`，归 runner——**策略无权重置靶场**（`:322`）。
所以这里不发任何请求：`reset()` 清掉本地的预算视图缓存，下一次探测的回复会
把它刷新回来。

## 预算视图是**捎带**回来的

`budget_left` 挂在每条回复的信封上（见 `protocol.Reply`）。策略改自己那份
副本不会影响评分——**权威账本在 runner 侧**（`v6 (d)`）。捎带而不是按包查询，
是因为 `v6 (d)` 明令「不按包查询」：那会把并发池重新串行化。捎带连一次额外
往返都不花。

## 种子从哪来、为什么**不**另开一条通道（T6a）

`seed` 是 T6a 定下的接口决定，它和 `prefixes`、预算视图走的是**同一条**路：
握手。备选是「runner 用 `spawn_strategy(extra_env=...)` 把种子塞进环境变量」，
它更便宜（不动协议、不动版本号），但**被否掉了**，理由不是洁癖：

1. **握手会拒绝，环境变量不会。** `PROTOCOL_VERSION` 从 2 起把「发不发种子」
   变成握手的一部分（`strategy_child._hello` 版本不匹配即 `SystemExit`）；
   环境变量漏设的现场是子进程里一句 `KeyError` 或**一个静默的默认值**，
   而两者都发生在策略跑到一半的时候。
2. **会话参数只有一条路。** `prefixes` 与预算视图已经在握手上了；给同一类
   信息再开一条进程级的旁路，等于把「一次运行是怎麼配起来的」拆成两处看。
3. **它在 ① 条边上可审计。** `tests/unit/test_isolation.py::TestEdgeOne` 对
   **服务端实际发出去的字节**下断言，所以「种子送出去了、真值没送出去」这条
   正反双断言落在同一个地方；走环境变量的话这条边上看不见它。

代价写在 `protocol.TRUTH_KEYS` 旁边：`seed` 是 ① 条边上**第二个**刻意不进黑名单
的字段（第一个是 `prefixes`），所以那条禁令的例外从一个变成两个，**每一处都要
有测试**。另一条代价记在设计文档的 T6a 完成块里：策略知道了配对种子，在 Stage 2
靶场状态由 seed 决定时就可能反推真值——那**不是这条通道能修的**（种子空间只有
30 个数，任何派生子都在枚举下可逆），它归 T15 的 `RouterShim`。
"""

from __future__ import annotations

import ipaddress
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from .protocol import (
    PROTO_ICMP6_ECHO,
    LineChannel,
    ProtocolError,
    Request,
    UnknownVerb,
    reply_from_wire,
    reply_to_wire,
    request_to_wire,
    response_from_wire,
)
from .memory import MemorySnapshot, memory_to_wire, normalize_memory
from .types import Response

_T = TypeVar("_T")


def _decode_samples(result: Any) -> list[Response]:
    if not isinstance(result, list):
        raise ProtocolError(f"sample 的结果是 {type(result).__name__}，期望 list")
    return [response_from_wire(item) for item in result]


def _decode_ack(result: Any) -> None:
    """`report` 的结果必须是 `null`——有返回值说明 runner 与客户端已经不同步。"""
    if result is not None:
        raise ProtocolError(f"report 的结果应是 null，得到 {type(result).__name__}")
    return None


def _wire_safe_state(value: object) -> object:
    """把规范化状态变成可 JSON 的等价值；集合排序后仍是集合语义。"""

    if isinstance(value, Mapping):
        return {str(key): _wire_safe_state(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(value)  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(_wire_safe_state(item) for item in value)
    return value


class SurfaceError(RuntimeError):
    """runner 侧拒绝了这次请求。

    它**不是** `Response` 的一种——`Response` 描述的是**链路**上发生了什么，
    而这里是**协议/权限**层面的拒绝（越界前缀、未知动词、预算耗尽）。
    混进 `Response` 会让「预算用完了」在数据里长得像「地址不存在」。
    """


class ProbeSurface:
    """策略侧唯一能拿到的对象。**没有 `ground_truth`，没有任何真值字段。**

    Args:
        prefixes: **去重后的前缀列表** —— 任务陈述（要测哪些前缀）。**只此而已**：
            构造参数不得含任何 `*_response_rate`、不得含 config 路径、不得含靶场
            对象（设计方案 `:375` 的 (c) 条）。它在这里的**唯一用途**是给
            `probe()` / `sample()` 做归属检查，防止策略探到任务范围之外去。
        channel: 通往 runner 的 stdio 通道。**已经 `hello` 过**——握手由
            `strategy_child` 负责，因为那是「子进程启动」的事，不是接口的事。
        budget_left: 握手回复里带回来的**初始预算视图**。**必须由
            `strategy_child` 传进来，不能省**——见下面那条。
        seed: 握手回复里带回来的**会话种子**（T6a / 协议 v2）。与 `prefixes`
            同属**任务陈述**那一类：它是这次运行的一组参数，不是前缀的属性。
            见 `seed` 属性与 `protocol.TRUTH_KEYS` 上方那段。
    """

    # ── 为什么 `budget_left` 是个构造参数（T13 的接口发现）──────────────────
    #
    # 握手的回复里本来就带 `budget_left`（`protocol.Reply` 的字段，由
    # `SurfaceServer.handle` 填），但 `ProbeSurface` 原先只在 `_call()` 里更新
    # 它，而 `_call()` 要等第一次探测才发生。于是**第一个策略（B0）发现自己
    # 拿不到预算**：`surface.budget_left is None`，而它要在发第一个包之前就决定
    # 「每个前缀探几次」。
    #
    # 绕开的写法是有的（先探一次再读 `budget_left`，反推出总量），但那等于把
    # 一个接口缺口编码进每一个策略里，而真正该知道这件事的地方是握手。
    # `v6 (d)` 已经把权威账本放在 runner 侧；这里只是把**它已经送出来的那份
    # 视图**接上，没有新增任何真值通路。所有后续策略（B1/B4/B5/M1/M2/A1/A2/A3）
    # 都要在开跑前知道自己有多少预算，所以在第一个策略这里修掉最便宜。

    def __init__(
        self,
        prefixes: Sequence[str],
        channel: LineChannel,
        *,
        budget_left: int | None = None,
        seed: int,
        epoch_size: int,
        memory: Mapping[str, object] | None = None,
    ) -> None:
        """
        `seed` **必填、无默认值**（T6a / 协议 v2）：它由握手带回来，是这套实验里
        每一个需要随机源的行（B1/B4/M1/M2/A1/A2/A3）唯一的可复现依据。漏传的唯一
        替代写法是猜一个，而两种猜法都静默：猜固定值会让所有 seed 跑出同一条
        轨迹，猜 `None` 会让 `rng()` 退化。这与 `SurfaceServer.sampler` 必填
        是同一条原则（V6-9）——**漏传必须炸**。
        """
        if not prefixes:
            raise ValueError("prefixes 不能为空——没有任务范围就不是一次实验")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError(f"seed 必须是整数，得到 {seed!r}")
        if not isinstance(epoch_size, int) or isinstance(epoch_size, bool) or epoch_size < 1:
            raise TypeError(f"epoch_size 必须是正整数，得到 {epoch_size!r}")
        self._networks = tuple(ipaddress.ip_network(p, strict=False) for p in prefixes)
        #: 去重后的前缀列表，原样保留给策略读（它是任务陈述，不是真值）。
        self.prefixes: tuple[str, ...] = tuple(dict.fromkeys(prefixes))
        self._channel = channel
        self._next_id = 0
        self._budget_left: int | None = budget_left
        self._seed = seed
        self._epoch_size = epoch_size
        self._memory = normalize_memory(memory, self.prefixes)

    # ── 探测 ────────────────────────────────────────────────────────────

    def probe(self, dst: str, proto: str, hop_limit: int = 64) -> Response:
        """对单个地址发一次探测。

        `proto` **必填**：Stage 1/2 只支持 `"icmp6-echo"`（外加 Stage 2 的
        RouterShim 不改协议）。必填而不是默认，是因为这个参数在契约里是
        「选什么工具」的落点——将来加协议时，**漏传必须炸**而不是静默退回 echo。
        """
        self._require_supported(proto)
        self._require_in_scope(dst)
        return self._call("probe", {"dst": dst, "proto": proto, "hop_limit": hop_limit}, response_from_wire)

    def sample(self, prefix: str, n: int) -> list[Response]:
        """在 `prefix` 里取 `n` 个地址探测。

        **地址怎么取由 runner 侧决定**（`SurfaceServer` 的 `sampler` 参数）。
        放在策略侧会让「均匀随机 vs offset 规则」变成策略能自由改的东西——
        而 6Seeks Theorem 1 说的正是 offset 规则决定了一个 `/x` 前缀里最多
        探几次（`B3` bit-reversal 就是它的实现）。那是**策略选择的变量**，
        不是接口的实现细节。
        """
        if n < 1:
            raise ValueError(f"n 至少为 1，得到 {n}")
        self._require_in_scope(prefix)
        return self._call("sample", {"prefix": prefix, "n": n}, _decode_samples)

    def report(self, prefix: str, aliased: bool) -> None:
        """声明「我判定这个前缀是 aliased」——`aliased_caught` 的**唯一**来源。

        v6 加这个通道的理由（设计方案 `:488`）：`probe`/`sample` 只回观测、
        不回判断，于是这个 co-primary 端点只能由裁判从行为里**猜**，而
        「猜它认出来了」与「它真的认出来了」是两件事。判定必须以它为准。

        **注意方向**：这条消息是策略→runner，所以它带 `aliased` 是应该的——
        `protocol.TRUTH_KEYS` 只管反方向。
        """
        self._require_in_scope(prefix)
        self._call("report", {"prefix": prefix, "aliased": bool(aliased)}, _decode_ack)

    def remember(self, memory: Mapping[str, object]) -> None:
        """回写**自己的**观测计数，供下一次 run 的 M2 使用。

        记忆只允许每个前缀的 `pulls` / `rewards`。runner 会再校验一次，
        防止策略进程绕过本地检查塞入真值字段、越界前缀或其他策略的数据。
        """
        normalized = normalize_memory(memory, self.prefixes)
        self._call("remember", {"memory": memory_to_wire(normalized, self.prefixes)}, _decode_ack)

    def decide(
        self,
        *,
        prompt: str,
        state: Mapping[str, object],
        tool_pool: Mapping[str, str],
    ) -> tuple[str, bool]:
        """向 runner 的决策缓存/模型代理要一条原始动作。

        策略进程拿不到缓存文件，也不直接联网。返回 `(raw, cache_hit)`；
        schema 解析仍由策略侧 `parse_action()` 在关键路径上完成。
        """

        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt 必须是非空字符串")
        if not isinstance(state, Mapping):
            raise TypeError("state 必须是映射")
        if not isinstance(tool_pool, Mapping):
            raise TypeError("tool_pool 必须是映射")

        def decode(result: Any) -> tuple[str, bool]:
            if not isinstance(result, dict):
                raise ProtocolError("decide 的结果必须是对象")
            if set(result) != {"raw", "cache_hit"}:
                raise ProtocolError(f"decide 结果字段不匹配：{sorted(result)}")
            raw = result["raw"]
            hit = result["cache_hit"]
            if not isinstance(raw, str) or not raw.strip():
                raise ProtocolError("decide 的 raw 必须是非空字符串")
            if not isinstance(hit, bool):
                raise ProtocolError("decide 的 cache_hit 必须是 bool")
            return raw, hit

        return self._call(
            "decide",
            {
                "prompt": prompt,
                "state": _wire_safe_state(state),
                "tool_pool": dict(tool_pool),
            },
            decode,
        )

    def reset(self) -> None:
        """清空**策略侧**的记忆与预算视图。不重置靶场，不发请求。

        见模块 docstring：靶场归 `Oracle.reset_target(seed)`。把两者合成一个
        方法正是 v6-2 拆掉的那个形状（`:322`）。
        """
        self._budget_left = None

    # ── 只读视图 ────────────────────────────────────────────────────────

    @property
    def budget_left(self) -> int | None:
        """预算视图的**本地副本**，随回复刷新；未握手前是 `None`。

        握手之后就是**已知**的（构造参数带进来的那份），所以策略在发第一个
        包之前就能读到它——见 `__init__` 的 `budget_left` 参数。

        它是**视图不是账本**：改它不影响任何东西，因为权威账本在 runner 侧
        （`v6 (d)`，`:397`）。做成只读属性而不是可写字段，是为了让
        「策略能不能改自己的预算」这件事根本没有代码路径。
        """
        return self._budget_left

    @property
    def seed(self) -> int:
        """这次会话的种子。**策略侧唯一的可复现随机源**（T6a / 协议 v2）。

        它**不是真值**，而且这个区别是承重的——见 `protocol.TRUTH_KEYS` 上方
        那段：与它同名近似的 `seed_map`（在表里）是「哪个前缀公布了哪些种子
        地址」，那是答案；`seed` 只是一个让随机源可复现的整数。

        做成只读属性而不是可写字段，理由与 `budget_left` 一样：改它不影响
        任何东西（runner 侧那份才是权威），但可写字段会让「策略以为自己换了
        seed」这件事有代码路径。
        """
        return self._seed

    @property
    def epoch_size(self) -> int:
        """本实验每次决策覆盖的探测数，由 runner 从 config 经握手送入。"""

        return self._epoch_size

    @property
    def memory(self) -> MemorySnapshot:
        """本 run 起始时的自观测记忆副本；改它不会影响任何权威状态。"""

        return {
            prefix: {"pulls": entry["pulls"], "rewards": entry["rewards"]}
            for prefix, entry in self._memory.items()
        }

    def rng(self) -> random.Random:
        """**这次会话的随机源。** 每个需要随机性的策略都从这里取，不要另起。

        三条规矩，各自都对应一种会静默的写法：

        1. **只用 `rng.random()`**，不用 `randrange` / `shuffle` / `sample`。
           Python 只保证 `random()` 那条序列跨版本不变（生成器与 seeding 兼容性
           是官方保证的），`randrange` 之类**不保证**——用它会让「同 seed 重跑
           逐包相同」在升一次 Python 之后悄悄失效。
        2. **每次调用返回一个新的 `Random`**：同一个 seed 与同一个调用序列给出
           同一条序列，所以「策略按什么顺序取随机数」是可复现的，而不是取决于
           谁先调了它。
        3. **它不是靶场的随机源。** 靶场的状态归 `Oracle.reset_target(seed)`
           （runner 侧）；这里给的是**策略自己那份**，改不了靶场。
        """
        return random.Random(self._seed)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _call(self, verb: str, args: dict[str, object], decode: Callable[[Any], _T]) -> _T:
        """发一条请求、等一条回复、把结果解成领域对象。

        `decode` 收的是 **wire 形态**（dict / list），不是已经解好的对象——
        一开始写成「传一个类型、`isinstance` 一下」是错的：`probe` 的结果是
        `Response` 的**线格式**（一个 dict），`isinstance(dict, Response)` 恒假。
        解码必须显式发生在这一层，否则「跨边的字段白名单」就绕过去了
        （`response_from_wire` 才是拒绝白名单外字段的地方）。
        """
        request = Request(verb=verb, args=args, id=self._next_id)
        self._next_id += 1
        self._channel.send(request_to_wire(request))

        raw = self._channel.recv()
        if raw is None:
            raise SurfaceError("runner 关掉了通道（进程结束了？）")
        reply = reply_from_wire(raw)
        if reply.id != request.id:
            # 同步通道上错配是**静默**错误：拿到的结果属于别的探测。
            raise ProtocolError(f"回复 id {reply.id} 对不上请求 id {request.id}")

        self._budget_left = reply.budget_left
        if not reply.ok:
            raise SurfaceError(f"runner 拒绝 {verb}: {reply.error}")
        return decode(reply.result)

    def _require_supported(self, proto: str) -> None:
        if proto != PROTO_ICMP6_ECHO:
            raise ValueError(
                f"不支持的 proto {proto!r}；当前只有 {PROTO_ICMP6_ECHO!r}"
                "（加协议时改这里，并同步 protocol.PROTOCOL_VERSION）"
            )

    def _require_in_scope(self, addr: str) -> None:
        """目标必须落在构造时给的前缀里。

        这是 `prefixes` 参数存在的**唯一**理由。没有它，策略可以探任意地址——
        那既越出任务范围，也让「跑了多少包」的预算口径失去意义。
        """
        try:
            ip = ipaddress.ip_address(addr if "/" not in addr else addr.split("/")[0])
        except ValueError as exc:
            raise ValueError(f"不是合法地址: {addr!r}") from exc
        if not any(ip in net for net in self._networks):
            raise ValueError(
                f"{addr} 不在任务范围内的任何前缀里；"
                "越界探测会被 runner 拒绝，别浪费一次往返"
            )


__all__ = ["ProbeSurface", "SurfaceError", "UnknownVerb"]
