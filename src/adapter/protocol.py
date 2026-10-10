"""策略进程 ↔ runner 进程的 stdio 协议 —— 数据流图上**第 ① 条边**（T2(c) / D7）。

设计方案 `:1143` 把这条边写成一条硬约束：

> **①上不得出现真值**——策略侧只能看到 `Response` 的八个字段。

散文里的约束会随实现漂移，所以这里把它做成**两条可执行的规则**：

1. **白名单**：`response_to_wire()` 只发出 `WIRE_RESPONSE_FIELDS` 里的键；
   `response_from_wire()` **拒绝**任何白名单外的键——不是忽略，是拒绝。
   于是「服务端不小心多带了一个字段」不会变成客户端一个静默的 `**kwargs`。
2. **黑名单**：`assert_no_truth_keys()` 递归检查每一个**服务端→客户端**的
   载荷，键名命中真值词汇（`aliased` / `*_rate` / `ground_truth` …）就抛。
   它在服务端的写路径里调用，所以**加一条新消息时不可能忘记**。

> **黑名单只管服务端→客户端这一个方向。** `report(prefix, aliased)` 是
> **客户端→服务端**，它当然带 `aliased`——那是策略在**声明自己的判断**，
> 不是 runner 在泄漏答案。把两个方向混成一条规则会让 `report` 无法实现，
> 而那正是 v6 特意为 `aliased_caught` 加的通道。方向必须写死在函数上。

## 帧格式

一行一个 JSON 对象，UTF-8，`\n` 结尾。不做二进制、不做压缩：这条边上是
每秒几十次的请求，可读性（`strace` 一眼能看）比那点开销值钱得多，而且
**载荷可读本身就是一条审计能力**。

**为什么是 `id` 而不是隐式顺序**：`ProbeSurface` 目前是同步的，顺序就够用。
带上 `id` 是为了让客户端能**断言拿到的是自己那条请求的回复**——一条错配的
回复在同步客户端里会表现为「结果对了」而实际属于别的探测，那是静默错误。
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .types import Response

#: 协议版本。**任何一端的字段增删都要动它**——版本不匹配时必须显式拒绝，
#: 而不是"能解析就继续"。跨进程协议静默降级的表现是数据错位（字段还在，
#: 语义变了），那比直接崩难查得多。
#:
#: **v2（T6a，2026-10-10）**：`hello` 的回复多了 `seed`（见 `TRUTH_KEYS` 上方那段）。
#: 这一格才是这个常量存在的意义：v1 的 runner 不会发种子，而 v1 的 B1/B4 会
#: 因为拿不到可复现的随机源**跑出不可复现的数**——版本号不同则握手当场拒绝，
#: 那种运行根本没有机会产生数字。
PROTOCOL_VERSION = 2

#: 跨过第 ① 条边的**全部**字段（设计方案 `:1143` 的「八个字段」）。
WIRE_RESPONSE_FIELDS = frozenset(
    {"status", "dst", "src", "error", "icmp_type", "icmp_code", "hop_limit", "rtt_ms"}
)

#: 真值词汇。服务端→客户端的载荷里出现任一即抛。
#:
#: `prefixes` **不在**表里，而且必须不在：它是**任务陈述**（要测哪些前缀），
#: 策略不知道它就无法工作。被剥掉的是前缀的**属性**（响应率），不是前缀本身。
#:
#: **`seed` 也不在表里，而且同样必须不在（T6a，v2）。** 它是**这次会话的种子**：
#: `(config, seed)` 是配对单位（`:559`），也是策略那一侧唯一可复现的随机源
#: ——`B1` 均匀随机、`B4` bandit 的平局、`M1/M2` 的记忆落点全都由它派生。
#: 它与 `seed_map`（**在表里**）不是一回事：`seed_map` 是「哪个前缀公布了哪些
#: 种子地址」，那是**答案**；`seed` 只是一个让随机源可复现的整数。
#: 两件事的差别是承重的——把 `seed` 加进这张表会让 B1/B4 在**握手上**就炸，
#: 而炸的理由是一句读起来像对的禁令。**这条注释与 `TestEdgeOne` 里那条
#: 正反双断言一起，是「它为什么不是真值」的全部依据。**
TRUTH_KEYS = frozenset(
    {
        "aliased",
        "ground_truth",
        "n_routers",
        "rates",
        "routers",
        "seed_map",
        "topology",
    }
)

#: 真值词汇的后缀规则。上游 config 的字段叫 `*_response_rate` / `eui_rate` /
#: `lower_rate` / `higher_rate` / `default_rate`——枚举不完，所以按后缀拦。
TRUTH_KEY_SUFFIXES = ("_rate",)

#: 客户端可以发的动词。**允许清单，不是禁止清单**：未知动词一律拒绝，
#: 免得 runner 侧将来多加一个动词时策略进程能悄悄用上它。
CLIENT_VERBS = frozenset({"hello", "probe", "sample", "report"})

#: `probe()` 的 `proto` 参数当前唯一支持的值（Stage 1/2 都是 ICMPv6 echo）。
PROTO_ICMP6_ECHO = "icmp6-echo"


class ProtocolError(Exception):
    """这条边上的协议错误。**任何形态都不得被当成一次「探测失败」。**"""


class TruthLeak(ProtocolError):
    """服务端正在往策略进程写一个真值字段。

    它必须炸，不能只 warn：这条边是 D7 的全部依靠，一次静默泄漏会让整组
    实验作废，而作废要到跑完才被发现（设计方案 `:401`）。
    """


class UnknownVerb(ProtocolError):
    """客户端发了一个不在 `CLIENT_VERBS` 里的动词。"""

    def __init__(self, verb: object) -> None:
        super().__init__(f"未知动词 {verb!r}；允许的是 {sorted(CLIENT_VERBS)}")
        self.verb = verb


def _is_truth_key(key: str) -> bool:
    return key in TRUTH_KEYS or key.endswith(TRUTH_KEY_SUFFIXES)


def assert_no_truth_keys(payload: Any, *, where: str = "载荷") -> None:
    """递归检查一个**服务端→客户端**的载荷里没有真值键。

    Raises:
        TruthLeak: 命中 `TRUTH_KEYS` 或后缀规则。
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str) and _is_truth_key(key):
                raise TruthLeak(
                    f"{where}里出现了真值字段 {key!r}——它正从 runner 流向策略进程，"
                    "违反数据流图第 ① 条边（设计方案 `:1143`）"
                )
            assert_no_truth_keys(value, where=f"{where} 的 {key!r}")
    elif isinstance(payload, (list, tuple)):
        for i, item in enumerate(payload):
            assert_no_truth_keys(item, where=f"{where}[{i}]")


# ── Response ↔ wire ─────────────────────────────────────────────────────────
#
# 下面这条断言是这一层存在的理由：**给 `Response` 加字段时会被迫在这里做决定**。
# 没有它的话，新字段既不会自动上线，也不会报错——它会**静默地不跨边**，
# 于是策略侧永远看到 None，而没有任何一处会红。

_RESPONSE_DECLARED = frozenset(Response.__dataclass_fields__)
if _RESPONSE_DECLARED != WIRE_RESPONSE_FIELDS:  # pragma: no cover - 加字段时才会走到
    raise AssertionError(
        "`Response` 的字段与跨进程白名单不一致：\n"
        f"  只在 Response 里：{sorted(_RESPONSE_DECLARED - WIRE_RESPONSE_FIELDS)}\n"
        f"  只在白名单里：{sorted(WIRE_RESPONSE_FIELDS - _RESPONSE_DECLARED)}\n"
        "加/删字段时**必须**同步改 WIRE_RESPONSE_FIELDS，并想清楚新字段"
        "会不会泄漏真值（设计方案 `:1143`）。"
    )


def response_to_wire(response: Response) -> dict[str, Any]:
    """`Response` → 可 JSON 的 dict。**只发白名单字段。**"""
    return {name: getattr(response, name) for name in sorted(WIRE_RESPONSE_FIELDS)}


def response_from_wire(payload: Mapping[str, Any]) -> Response:
    """wire dict → `Response`。

    **多一个键就拒绝。**「忽略未知键」在这里是最坏的选择：它让服务端的一次
    真值泄漏在客户端表现为「一切正常」——而这条边正是 D7 唯一挡住泄漏的地方。
    """
    unknown = set(payload) - WIRE_RESPONSE_FIELDS
    if unknown:
        raise ProtocolError(
            f"回复里有白名单外的字段 {sorted(unknown)}；"
            "这台 runner 可能版本不匹配，或者正在这条边上夹带东西"
        )
    missing = {"status", "dst"} - set(payload)  # `Response` 的必填字段
    if missing:
        raise ProtocolError(f"回复缺少必填字段 {sorted(missing)}")
    return Response(**payload)


# ── 请求 / 回复 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Request:
    verb: str
    args: Mapping[str, Any]
    id: int


@dataclass(frozen=True, slots=True)
class Reply:
    """一条回复。

    `budget_left` 挂在**信封**上而不是 `result` 里：它是通道级的元信息，
    不是 `Response` 的一部分。放这里面还顺带解决了预算视图的回路问题——
    策略的预算视图**搭在每次探测回复上捎回来**，不额外发一次「查预算」请求。
    `v6 (d)` 要求「按 epoch 预留、不按包查询」，理由是按包查询会把并发池
    重新串行化；**捎带比按包查询更省**（零次额外往返），而要粗化成 epoch 粒度
    只需将来只在 epoch 边界刷新这个字段——协议不用变。
    """

    id: int
    ok: bool
    result: Any = None
    error: str | None = None
    budget_left: int | None = None


def encode(obj: Mapping[str, Any]) -> bytes:
    """一行 JSON。`separators` 去掉空格：让它更像一行日志而不是一段文本。"""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: str | bytes) -> dict[str, Any]:
    """解析一行。任何畸形都抛 `ProtocolError`，不返回 None。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是合法的 JSON 行: {line!r}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"一行必须是一个 JSON 对象，得到 {type(obj).__name__}")
    return obj


def request_to_wire(request: Request) -> dict[str, Any]:
    return {"id": request.id, "verb": request.verb, "args": dict(request.args)}


def request_from_wire(payload: Mapping[str, Any]) -> Request:
    try:
        verb = payload["verb"]
        req_id = payload["id"]
    except KeyError as exc:
        raise ProtocolError(f"请求缺少字段 {exc}") from exc
    if not isinstance(verb, str):
        raise ProtocolError(f"verb 必须是字符串，得到 {type(verb).__name__}")
    if verb not in CLIENT_VERBS:
        raise UnknownVerb(verb)
    if not isinstance(req_id, int) or isinstance(req_id, bool):
        raise ProtocolError(f"id 必须是整数，得到 {req_id!r}")
    args = payload.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("args 必须是对象")
    return Request(verb=verb, args=args, id=req_id)


def reply_to_wire(reply: Reply) -> dict[str, Any]:
    out: dict[str, Any] = {"id": reply.id, "ok": reply.ok}
    if reply.ok:
        out["result"] = reply.result
    else:
        out["error"] = reply.error
    if reply.budget_left is not None:
        out["budget_left"] = reply.budget_left
    return out


def reply_from_wire(payload: Mapping[str, Any]) -> Reply:
    if "id" not in payload or "ok" not in payload:
        raise ProtocolError(f"回复缺少 id/ok: {sorted(payload)}")
    ok = payload["ok"]
    if not isinstance(ok, bool):
        raise ProtocolError("ok 必须是布尔")
    error = payload.get("error")
    if not ok and not isinstance(error, str):
        raise ProtocolError("失败的回复必须带字符串 error")
    left = payload.get("budget_left")
    if left is not None and not isinstance(left, int):
        raise ProtocolError(f"budget_left 必须是整数，得到 {left!r}")
    return Reply(id=payload["id"], ok=ok, result=payload.get("result"), error=error, budget_left=left)


# ── 通道 ────────────────────────────────────────────────────────────────────


class LineChannel(Protocol):
    """一条双向的行通道。子进程侧就是 `sys.stdin` / `sys.stdout`。"""

    def send(self, obj: Mapping[str, Any]) -> None: ...
    def recv(self) -> dict[str, Any] | None:
        """读一行；**返回 `None` 表示对端关掉了通道**（EOF）。

        用 `None` 而不是抛异常表达 EOF：子进程正常结束时 runner 就该看到
        EOF，那不是一个错误。反之若把 EOF 抛成异常，runner 的常规收尾路径
        和崩溃路径会混在一起。
        """
    def close(self) -> None: ...


class JsonLineChannel:
    """在任意一对文本流上的 `LineChannel`。"""

    def __init__(self, reader: Iterable[str], writer: Any) -> None:
        self._lines: Iterator[str] = iter(reader)
        self._writer = writer

    def send(self, obj: Mapping[str, Any]) -> None:
        self._writer.write(encode(obj).decode("utf-8"))
        self._writer.flush()

    def recv(self) -> dict[str, Any] | None:
        for line in self._lines:
            if not line.strip():
                continue  # 空行是噪声，不是帧
            return decode(line)
        return None

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:  # pragma: no cover - 关流的失败不该盖住真正的原因
            pass


class MemoryChannel:
    """进程内成对的 `LineChannel` —— 让协议与 `ProbeSurface` 的测试不必起子进程。

    `on_request` 是服务端：客户端每 `send()` 一次就同步调它一次，把返回的
    回复排进客户端队列。**服务端写的每一帧都留在 `sent_to_client` 里**，
    于是测试可以对「服务端实际发出去的字节」下断言——这正是第 ① 条边
    唯一能被验的地方（见 `tests/unit/test_isolation.py`）。

    它**不模拟**进程边界（同一个解释器里两边互相都 import 得到），所以它测
    不了隔离。**隔离必须用真子进程测**，否则测到的是「我们没写错」而不是
    「边界在那儿」。
    """

    def __init__(self, on_request: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._on_request = on_request
        self._to_client: deque[dict[str, Any]] = deque()
        self._to_server: deque[dict[str, Any]] = deque()
        #: 服务端→客户端发过的**全部**帧，按序，不清空。
        self.sent_to_client: list[dict[str, Any]] = []

    def client_side(self) -> LineChannel:
        return _Endpoint(self, side="client")

    def server_side(self) -> LineChannel:
        return _Endpoint(self, side="server")

    def _on_client_send(self, frame: dict[str, Any]) -> None:
        """客户端发出的一帧到达服务端；`on_request` 同步产出回复。

        **回复必须走 `_emit`**，否则 `sent_to_client` 会漏掉它——而审计
        副本漏帧是最坏的一种漏：第 ① 条边的黑名单断言会变成一条**永远通过**
        的测试，看起来还在守着。
        """
        self._to_server.append(frame)
        if self._on_request is not None:
            self._emit(self._on_request(frame))

    def _emit(self, frame: dict[str, Any]) -> None:
        """服务端→客户端的一帧：留审计副本 + 入队。"""
        self.sent_to_client.append(frame)
        self._to_client.append(frame)


class _Endpoint:
    def __init__(self, owner: MemoryChannel, *, side: str) -> None:
        self._owner = owner
        self._side = side

    def send(self, obj: Mapping[str, Any]) -> None:
        frame = dict(obj)
        if self._side == "client":
            self._owner._on_client_send(frame)
        else:
            self._owner._emit(frame)

    def recv(self) -> dict[str, Any] | None:
        queue = self._owner._to_client if self._side == "client" else self._owner._to_server
        return queue.popleft() if queue else None

    def close(self) -> None:
        """在内存通道里关流是空操作 —— 没有真正的 fd 可关。"""
