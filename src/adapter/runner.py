"""runner 侧的服务端 —— 数据流图 `:1116` 那个框（T2(c) / D7）。

这个进程持有四样策略进程**不能**有的东西（`:1117-1121`）：

1. **探测出口**（`RawIcmp6Iface` + `ProbePool`，经 `executor` 注入）
2. **`Oracle`** —— 唯一的真值出口
3. **权威预算账本**（`BudgetLedger`）—— `v6 (d)` 明令它必须在这一侧
4. **地址采样器** —— `sample()` 里「取哪些地址」由这里定，不由策略定

## 结构：`handle()` 是纯的，`serve()` 只是把通道接上去

`handle(wire_request) -> wire_reply` 不碰任何流，所以它的全部分支（包括
「未知动词」「越界前缀」「预算耗尽」）都能用普通单测钉住。`serve()` 只做
收发与 EOF 收尾——**把 I/O 挤到最薄的一层**，是因为这条边上最容易出的错
（多带一个真值字段、回复错配到别的请求）都不是 I/O 问题。

## 出边的每一帧都过黑名单

`_write()` 调 `protocol.assert_no_truth_keys()`。放在**唯一的写口**上，
于是将来加一条新消息时**不可能**忘记检查——而漏检的表现是静默泄漏，
要到整组实验跑完才被发现（`:401`）。

## 为什么 `sampler` 必填无默认值

它是 `sample()` 里「在 `/x` 前缀里取哪 n 个地址」的规则。**这条规则是策略
要选的变量，不是接口的实现细节**：6Seeks Theorem 1 说的就是 offset 规则
决定了一个 `/x` 里最多探几次，而 `B3` bit-reversal 正是它的实现
（设计方案 `:814`）。给它一个默认值，等于把「所有策略其实用同一种采样」
写成静默行为，而那会直接抹掉一组基线的差异。与 `max_in_flight` 同一条
原则（V6-9）：**漏传必须炸。**
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from . import protocol
from .oracle import Oracle
from .probe_pool import ProbeRun, ProbeTarget
from .protocol import PROTOCOL_VERSION, Request, UnknownVerb
from .types import Response


class BudgetExhausted(Exception):
    """预算不够跑这一批。**它不得塌成 `sent_no_reply`。**"""


class ProbeExecutor(Protocol):
    """真正发包的东西。`ProbePool` 满足它；测试注入假实现。"""

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun: ...


class BudgetLedger:
    """**权威**预算账本。策略侧那份 `budget_left` 只是它的一个只读视图。

    `v6 (d)`（`:397`）：原文把预算记在 `ProbeSurface` 上，而 surface 是交给
    策略的对象——一个 `self.budget_left += 1000` 就绕过了，且 D7 的整个前提
    就是「**A2 会写代码**」。所以账本必须在这一侧，策略改自己那份不影响评分。
    """

    def __init__(self, total: int) -> None:
        if total < 1:
            raise ValueError(f"预算至少为 1，得到 {total}")
        self.total = total
        self.spent = 0

    @property
    def left(self) -> int:
        return self.total - self.spent

    def charge(self, n: int) -> None:
        """先扣后跑。**超了就抛，不得跑一半。**"""
        if n > self.left:
            raise BudgetExhausted(f"预算剩 {self.left}，这次要 {n}")
        self.spent += n


class SurfaceServer:
    """`ProbeSurface` 的 runner 侧。"""

    def __init__(
        self,
        *,
        executor: ProbeExecutor,
        oracle: Oracle,
        ledger: BudgetLedger,
        sampler: Callable[[str, int], Sequence[str]],
        prefixes: Sequence[str],
        seed: int,
        timeout_s: float | None = None,
    ) -> None:
        """
        Args:
            sampler: `(prefix, n) -> 地址序列`。**必填无默认值**，理由见模块 docstring。
            prefixes: 这次实验的任务范围。子进程握手时拿到的是它，`probe`/
                `sample` 的目标必须落在它里面。**注意这里没有 `*_response_rate`
                ——真值留在本进程**（设计方案 `:395` 的 (c) 条）。
            seed: 这次会话的种子（T6a / 协议 v2）。**必填无默认值**，与 `sampler`
                同一条原则：握手必须**无条件**把它送出去，所以这里不能有
                「没传就不发」这一支——一支存在，就有一条能让 B1/B4 悄悄变成
                不可复现的路径。它同时是策略侧随机源的种子，但**不是靶场重置
                用的那个**（后者由 `session.run_session` 调 `Oracle.reset_target`）。
            timeout_s: 只用于给子进程报一个信息性的预算口径，不参与判定。
        """
        if not callable(sampler):
            raise TypeError("sampler 必须是可调用对象")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError(f"seed 必须是整数，得到 {seed!r}")
        self.executor = executor
        self.oracle = oracle
        self.ledger = ledger
        self.sampler = sampler
        self.prefixes = tuple(dict.fromkeys(prefixes))
        if not self.prefixes:
            raise ValueError("prefixes 不能为空")
        self.seed = seed
        self.timeout_s = timeout_s
        #: `report()` 收到过的全部判定，按序。`aliased_caught` 的**唯一**来源
        #: （设计方案 `:488`）：评分器不得从行为里推断它。
        self.reports: list[tuple[str, bool]] = []
        #: 跑过的目标地址总数 —— 标定与对账读它。
        self.probes_run = 0

    # ── 请求分发 ────────────────────────────────────────────────────────

    def handle(self, wire_request: Mapping[str, Any]) -> dict[str, Any]:
        """一条请求 → 一条回复。**不碰流**，所以可单测。"""
        try:
            request = protocol.request_from_wire(wire_request)
        except UnknownVerb as exc:
            return protocol.reply_to_wire(
                protocol.Reply(id=wire_request.get("id", -1), ok=False, error=str(exc))
            )
        except protocol.ProtocolError as exc:
            return protocol.reply_to_wire(
                protocol.Reply(id=wire_request.get("id", -1), ok=False, error=str(exc))
            )

        try:
            result = self._dispatch(request)
        except (BudgetExhausted, ValueError, KeyError, TypeError, protocol.ProtocolError) as exc:
            # **被拒绝的请求必须是一条显式的错误回复**，不能悄悄跳过：
            # 策略侧收到 SurfaceError 才能把「预算/越界」与「地址不回包」分开。
            return protocol.reply_to_wire(
                protocol.Reply(id=request.id, ok=False, error=f"{type(exc).__name__}: {exc}")
            )

        return protocol.reply_to_wire(
            protocol.Reply(id=request.id, ok=True, result=result, budget_left=self.ledger.left)
        )

    def _dispatch(self, request: Request) -> Any:
        args = request.args
        if request.verb == "hello":
            # `seed` 与 `prefixes` 同属**任务陈述**那一类，不是真值——理由写在
            # `protocol.TRUTH_KEYS` 上方那段，本函数是它唯一的发出点。
            return {"protocol": PROTOCOL_VERSION, "prefixes": list(self.prefixes), "seed": self.seed}

        if request.verb == "probe":
            dst = str(args["dst"])
            self._require_in_scope(dst)
            self.ledger.charge(1)
            targets = [ProbeTarget(dst, hop_limit=int(args.get("hop_limit", 64)))]
            return protocol.response_to_wire(self._run_one(targets))

        if request.verb == "sample":
            prefix = str(args["prefix"])
            self._require_in_scope(prefix)
            n = int(args["n"])
            if n < 1:
                raise ValueError(f"n 至少为 1，得到 {n}")
            self.ledger.charge(n)
            addresses = list(self.sampler(prefix, n))
            if len(addresses) != n:
                raise ValueError(f"sampler 给了 {len(addresses)} 个地址，要的是 {n}")
            run = self._run([ProbeTarget(a) for a in addresses])
            return [protocol.response_to_wire(r) for r in run.responses]

        if request.verb == "report":
            prefix = str(args["prefix"])
            self._require_in_scope(prefix)
            aliased = args["aliased"]
            if not isinstance(aliased, bool):
                raise TypeError(f"aliased 必须是布尔，得到 {type(aliased).__name__}")
            self.reports.append((prefix, aliased))
            return None

        raise UnknownVerb(request.verb)  # pragma: no cover - 已被 request_from_wire 挡掉

    def _run(self, targets: Sequence[ProbeTarget]) -> ProbeRun:
        self.probes_run += len(targets)
        return self.executor.run(targets)

    def _run_one(self, targets: Sequence[ProbeTarget]) -> Response:
        run = self._run(targets)
        if len(run.responses) != 1:
            raise AssertionError(f"单目标探测收到 {len(run.responses)} 条结果")
        return run.responses[0]

    def _require_in_scope(self, addr: str) -> None:
        """目标必须落在任务范围内。

        **两侧都查是刻意的**：`ProbeSurface` 那边查是为了省一次往返，这边查
        是因为**策略进程是敌对环境**（A2 会写代码，`ProbeSurface` 可以被绕过），
        所以权威判定必须在 runner 侧。
        """
        ip = ipaddress.ip_address(addr.split("/")[0])
        nets = [ipaddress.ip_network(p, strict=False) for p in self.prefixes]
        if not any(ip in net for net in nets):
            raise ValueError(f"{addr} 不在任务范围 {self.prefixes} 内")

    # ── 流 ──────────────────────────────────────────────────────────────

    def serve(self, channel: protocol.LineChannel) -> None:
        """把通道接到 `handle()` 上，直到对端关流。

        `recv()` 返回 `None` 表示子进程结束了——那是**正常收尾**，不是错误。
        """
        while True:
            raw = channel.recv()
            if raw is None:
                return
            self._write(channel, self.handle(raw))

    @staticmethod
    def _write(channel: protocol.LineChannel, frame: Mapping[str, Any]) -> None:
        """**唯一的写口** —— 真值黑名单挂在这里（见模块 docstring）。"""
        protocol.assert_no_truth_keys(frame, where="服务端→客户端的回复")
        channel.send(frame)


# ── 子进程 ──────────────────────────────────────────────────────────────────


class StrategyProcess:
    """一个跑着的策略子进程。"""

    def __init__(self, popen: subprocess.Popen[str]) -> None:
        self._popen = popen
        #: 服务端的收尾异常。在 `wait()` 之前读它，能看到服务端**自己**崩了
        #: ——那种情况下子进程会拿到 EOF 而提前退出，看起来像「策略正常结束」。
        self.failure: BaseException | None = None
        self._server_thread: threading.Thread | None = None
        self._stderr_chunks: list[str] = []
        #: stderr 由一个**读线程**持续抽干，不是等到 `wait()` 之后再 `read()`。
        #: 管道缓冲区只有几十 KB：一个话多的策略（LLM 决策体尤其话多）在
        #: `wait()` 之前就把缓冲区写满，子进程会**阻塞在 write 上**，而父进程
        #: 正阻塞在 `wait()` 上——互相等死。这个死锁的现场看起来像「策略跑不动」，
        #: 排查方向完全错。
        self._stderr_thread: threading.Thread | None = None
        if popen.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(popen.stderr,), daemon=True
            )
            self._stderr_thread.start()

    def _drain_stderr(self, stream: Any) -> None:
        for line in stream:
            self._stderr_chunks.append(line)

    def wait(self, timeout: float | None = None) -> int:
        code = self._popen.wait(timeout=timeout)
        if self._server_thread is not None:
            self._server_thread.join(timeout=timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=timeout)
        if self.failure is not None:
            raise self.failure
        return code

    def stderr(self) -> str:
        """子进程已经写出来的全部 stderr。**不阻塞**——后台线程在抽。"""
        return "".join(self._stderr_chunks)


def spawn_strategy(
    entry: str,
    *,
    server: SurfaceServer,
    cwd: str | None = None,
    python: str = sys.executable,
    extra_env: Mapping[str, str] | None = None,
) -> StrategyProcess:
    """起一个策略子进程，把 stdio 接到 `server` 上。

    Args:
        entry: 策略的入口，形如 `"pkg.module:run"` 或 `"pkg.module"`（取 `run`）。
            契约是 `run(surface) -> None`。

    **子进程的 stdout 就是那条边。** 所以 `strategy_child` 会把 `sys.stdout`
    换成 `sys.stderr`，策略里的 `print()` 因此污染不了协议——一个手滑的
    `print` 本来会把一行 JSON 劈成两半，症状是「协议解析偶发失败」。

    **强制 `PYTHONIOENCODING=utf-8`，而且只能这样强制。** Popen 的 `encoding=`
    只管父进程**这一侧**的包装；子进程按自己的 locale 决定怎么编码 stdout /
    stderr。在中文 Windows 上那是 GBK，于是协议里一发非 ASCII 字节，父进程按
    UTF-8 解就炸——而报错落在**父进程的读线程**里，现场看起来是「策略进程
    的 stderr 读不出来」，跟真正的原因（子进程的 locale）隔着一层。
    实测撞到过一次：策略里 `print("...")` 带中文即触发。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    popen = subprocess.Popen(
        [python, "-m", "src.adapter.strategy_child", "--entry", entry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    process = StrategyProcess(popen)

    assert popen.stdin is not None and popen.stdout is not None
    channel = protocol.JsonLineChannel(popen.stdout, popen.stdin)

    def _serve() -> None:
        try:
            server.serve(channel)
        except BaseException as exc:  # noqa: BLE001 - 要原样交给 wait() 报出来
            process.failure = exc

    thread = threading.Thread(target=_serve, name="surface-server", daemon=True)
    process._server_thread = thread
    thread.start()
    return process


__all__ = [
    "BudgetExhausted",
    "BudgetLedger",
    "ProbeExecutor",
    "StrategyProcess",
    "SurfaceServer",
    "spawn_strategy",
]
