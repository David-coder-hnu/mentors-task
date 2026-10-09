"""能力隔离 —— **T2(c)，T17 点名的四条不可失败项之一**（设计方案 `:1352`）。

D7 的强制手段是**进程边界**，理由是 `:389`：v5 删掉了「类型边界 + `hasattr`
断言」这条退路，因为「**A2 能写代码**」——一段生成的代码绕过一句 `hasattr`
是平凡的，而 A2 的全部卖点就是它写代码。

**于是问题变成：删掉 `hasattr` 之后，隔离靠什么被自动验证？** 这个文件是答案。
四条断言，各自防一类失效：

| 断言 | 防的是 |
|---|---|
| `TestImportGraph` | 策略进程的**运行时命名空间**里出现了真值类型/模块 |
| `TestEdgeOne` | 服务端→客户端的**字节**里出现了真值字段 |
| `TestRealProcessBoundary` | 上面两条只在「一个解释器里的两个对象」上成立，而不是真进程 |
| `TestBudgetIsNotLocal` | 策略改自己那份预算视图就能影响评分（`v6 (d)`） |

> **为什么非得跑真子进程。** `MemoryChannel` 里两边互相都 import 得到——
> 在那里断言「拿不到真值」，测到的是「我们没写错」，不是「边界在那儿」。
> 所以 `TestRealProcessBoundary` 起一个真的 `python -m`，让它**自己去够**
> 真值，把够到的东西报回来。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapter import protocol
from src.adapter.oracle import Oracle
from src.adapter.probe_pool import ProbeRun
from src.adapter.protocol import MemoryChannel, ProtocolError, TruthLeak
from src.adapter.runner import BudgetExhausted, BudgetLedger, SurfaceServer
from src.adapter.surface import ProbeSurface, SurfaceError
from src.adapter.truth import Stage1Truth
from src.adapter.types import Response

REPO_ROOT = Path(__file__).resolve().parents[2]

PREFIX = "2001:db8:dead::/64"
IN_RANGE = "2001:db8:dead::1"
OUT_OF_RANGE = "2001:db8:0bad::1"


# ── 假件 ────────────────────────────────────────────────────────────────────

FAKE_TRUTH = Stage1Truth(prefixes={PREFIX: {"aliased": True}})


def _oracle() -> Oracle:
    return Oracle(truth_source=lambda: FAKE_TRUTH, reset_target=lambda seed: None)


class FakeExecutor:
    """不出网卡；按地址的最后一段决定回不回包。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, targets) -> ProbeRun:
        dsts = [t.dst if hasattr(t, "dst") else str(t) for t in targets]
        self.calls.append(dsts)
        responses = tuple(
            Response(status="received", dst=d, src=d, hop_limit=64, rtt_ms=1.0)
            if d.endswith("::1")
            else Response(status="sent_no_reply", dst=d)
            for d in dsts
        )
        return ProbeRun(responses=responses, sent=len(dsts))


def make_server(**kwargs) -> SurfaceServer:
    options = dict(
        executor=FakeExecutor(),
        oracle=_oracle(),
        ledger=BudgetLedger(100),
        sampler=lambda prefix, n: [f"{prefix.rstrip(':')}::{i:x}" for i in range(1, n + 1)],
        prefixes=[PREFIX],
    )
    options.update(kwargs)
    return SurfaceServer(**options)  # type: ignore[arg-type]


def make_wired(server: SurfaceServer) -> tuple[ProbeSurface, MemoryChannel]:
    """接上一对 (surface, channel)；channel 里留着服务端发过的每一帧。"""
    channel = MemoryChannel(on_request=server.handle)
    return ProbeSurface([PREFIX], channel.client_side()), channel


def make_surface(server: SurfaceServer | None = None) -> ProbeSurface:
    return make_wired(server or make_server())[0]


# ── ① 导入图 ────────────────────────────────────────────────────────────────


class TestImportGraph:
    """`surface` 的导入图里不得出现真值模块。

    **这是 `hasattr` 退路被删掉之后，唯一还能自动验证的隔离形式。** v5 的
    理由是「A2 会写代码」——那反过来说：凡是**代码结构**能保证的，生成的代码
    也绕不过去。所以把一个原则性问题换成一个结构性断言不是降级。
    """

    def _child_import_graph(self) -> dict:
        """在干净子进程里导入 `surface`，报回 `sys.modules`。

        必须在**子进程**里做：pytest 进程自己早就 import 过 `oracle` 了，
        在它里面断言 `sys.modules` 只会永远失败（或永远通过，取决于顺序）。
        """
        code = (
            "import json, sys;"
            "import src.adapter.surface;"
            "print(json.dumps(sorted(m for m in sys.modules if 'mentors' in m or m.startswith('src.'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_surface_does_not_pull_in_the_oracle(self) -> None:
        modules = self._child_import_graph()
        leaked = [m for m in modules if m in {"src.adapter.oracle", "src.adapter.truth"}]
        assert not leaked, (
            f"导入 `surface` 会连带导入 {leaked}——"
            "策略进程的命名空间里出现了真值，违反 D7"
        )

    def test_surface_does_not_pull_in_the_probe_stack(self) -> None:
        """也不得带入 socket / 靶场那一侧。策略发包要走 runner，不是自己开 socket。"""
        modules = self._child_import_graph()
        leaked = [
            m
            for m in modules
            if m in {"src.adapter.probe_pool", "src.adapter.iface", "src.adapter.runner"}
        ]
        assert not leaked, f"导入 `surface` 会连带导入 {leaked}"

    def test_surface_module_has_no_truth_attribute(self) -> None:
        """补一条便宜的回归：`surface` 模块本身不导出真值名字。

        **这条不是强制手段**（v5 已删 `hasattr` 那条退路）——它只是防止
        「有人顺手 `from .truth import Stage1Truth` 加个类型注解」。真正的
        保证在上面的导入图断言里。
        """
        import src.adapter.surface as surface_module

        assert not hasattr(surface_module, "Stage1Truth")
        assert not hasattr(surface_module, "Oracle")
        assert not hasattr(ProbeSurface, "ground_truth")


class TestAgentImportGraph:
    """`src.agent` 的导入图里同样不得出现真值侧（T5 加的一条）。

    **为什么 `surface` 那条不够。** T5 往策略进程里又塞了一个包（决策层），
    而它和 `surface` 是两条独立的导入路径：`surface` 干净不代表 `agent` 干净。
    最容易踩的坑很具体——`src/scoring/` 里有一个现成的最长前缀匹配、现成的
    状态归一化，"顺手复用"看起来完全合理，而**那一层必须有真值**
    （见 `src/scoring/__init__.py`）。所以 `decide.Stage1State.prefix_for()`
    自带了一份只用 `ipaddress` 的匹配；它**不是重复代码，是 D7 的代价**，
    这条断言就是收那份代价的凭据。

    **必须在干净子进程里做**，理由同 `TestImportGraph`：pytest 进程自己早就
    import 过 `oracle` 与 `runner` 了。
    """

    #: 策略侧命名空间里**绝不能**出现的模块：真值本身的定义处
    #: （`oracle` / `truth`）、runner 侧的发包栈（`runner` / `probe_pool` / `iface`），
    #: 以及整个评分层（评分层的输入是真值，所以它的每一块都间接握着真值）。
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
            "import src.agent;"
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

    def test_agent_does_not_pull_in_truth_or_the_runner_side(self) -> None:
        modules = self._child_import_graph()
        leaked = [m for m in modules if m in self.FORBIDDEN]
        assert not leaked, (
            f"导入 `src.agent` 会连带导入 {leaked}——"
            "策略进程的命名空间里出现了真值 / runner 侧，违反 D7"
        )

    def test_agent_does_not_pull_in_the_scoring_layer(self) -> None:
        """单列一条：`src.scoring` 整层都在黑名单里，不只是它的某个子模块。

        写成前缀匹配而不是列名字，是因为**往 `src/scoring/` 里加一个新文件
        不该需要回来改这条测试**——那正是"名单会忘"的形状。
        """
        modules = self._child_import_graph()
        leaked = [m for m in modules if m == "src.scoring" or m.startswith("src.scoring.")]
        assert not leaked, f"导入 `src.agent` 会连带导入评分层 {leaked}——它必须有真值"

    def test_agent_cannot_reach_the_network(self) -> None:
        """**Stage 1 的决策层不许能联网。**（T5 加的一条）

        T5 的 `Completion` 是一个注入口（`decide.py`），Stage 1 的测试传假函数，
        于是"不接 API"不需要靠 code review 保证——**本包没有任何一个可以联网的
        import**。这条把它变成可断言的结构事实：一个 `socket` / `ssl` /
        `http.client` / `urllib.request` 都没被拖进来。

        > 为什么值得单列一条：一个"顺手 `import requests` 兜个底"的改动，
        > 会让**回放**在一份不完整的缓存上悄悄联网（见 `decide.py` 的
        > 离线回放模式）。那是一次没有任何人记录的调用，而报告里写着它是复现。
        """
        modules = self._child_import_graph()
        network = sorted(
            m
            for m in modules
            if m in {"socket", "ssl", "http.client", "urllib.request", "asyncio"}
            or m.split(".")[0] in {"requests", "httpx", "openai", "anthropic", "urllib3"}
        )
        assert not network, f"导入 `src.agent` 会连带导入网络客户端 {network}"


# ── ② 第 ① 条边上的字节 ─────────────────────────────────────────────────────


class TestEdgeOne:
    """服务端→客户端的**每一帧**都不得含真值字段（设计方案 `:1143`）。

    这条与导入图是两件事：导入图管「策略能不能拿到真值的**代码/对象**」，
    这条管「runner 有没有**发**真值给它」。两个方向都要堵。
    """

    def test_a_full_session_never_puts_truth_on_the_wire(self) -> None:
        """跑一遍全部动词，把服务端发出去的每一帧都过黑名单。

        这条**直接对字节下断言**，而不是对「服务端应该发什么」下断言——前者
        覆盖到将来新加的消息，后者只覆盖到想得到的那些。
        """
        surface, channel = make_wired(make_server())

        surface.probe(IN_RANGE, "icmp6-echo")
        surface.sample(PREFIX, 3)
        surface.report(PREFIX, True)

        # `ProbeSurface` 发三个动词 → 服务端回三帧。（`hello` 由
        # `strategy_child` 在握手时发，不经过 surface，单独在下面那条测里覆盖。）
        # 这条下限存在的理由：审计副本要是漏帧，下面那个循环会**一条都没查**
        # 而测试照样绿——一条永远通过的守卫比没有守卫更坏。
        assert len(channel.sent_to_client) >= 3, (
            f"服务端只发了 {len(channel.sent_to_client)} 帧，覆盖不全，这条断言是空的"
        )
        for frame in channel.sent_to_client:
            protocol.assert_no_truth_keys(frame, where="服务端→客户端的帧")

    def test_the_hello_frame_carries_prefixes_but_no_rates(self) -> None:
        """前缀列表是**任务陈述**，必须发；它的属性（响应率）必须不发。

        这条把 `TRUTH_KEYS` 里那个刻意的空档钉住：`prefixes` **不在**黑名单里，
        而且不能进去——策略不知道要测哪些前缀就无法工作。被剥掉的是前缀的
        **属性**，不是前缀本身（设计方案 `:375` 的 (c) 条）。
        """
        server = make_server()
        frame = server.handle({"id": 0, "verb": "hello", "args": {}})
        assert frame["ok"] and frame["result"]["prefixes"] == [PREFIX]
        assert "aliased" not in json.dumps(frame)

    def test_a_rate_field_would_be_caught_by_the_denylist(self) -> None:
        """黑名单要有牙：`*_rate` 必须被拦下。"""
        with pytest.raises(TruthLeak, match="response_rate"):
            protocol.assert_no_truth_keys(
                {"result": {"prefixes": {PREFIX: {"response_rate": 1.0}}}}
            )

    def test_aliased_is_caught_in_the_server_direction(self) -> None:
        with pytest.raises(TruthLeak, match="aliased"):
            protocol.assert_no_truth_keys({"result": {"aliased": True}})

    def test_aliased_is_allowed_in_the_client_direction(self) -> None:
        """`report()` 当然带 `aliased` —— 那是策略在**声明判断**，不是被泄漏答案。

        方向必须写死在函数上：把两个方向混成一条规则会让 `report` 无法实现，
        而那正是 v6 为 `aliased_caught` 特意加的通道（设计方案 `:488`）。
        """
        frame = make_server().handle(
            {"id": 1, "verb": "report", "args": {"prefix": PREFIX, "aliased": True}}
        )
        assert frame["ok"]
        # 反过来：服务端**回**给客户端的那一帧里不得有 aliased
        protocol.assert_no_truth_keys(frame, where="回复")

    def test_a_truth_smuggling_reply_is_rejected_by_the_client(self) -> None:
        """客户端**拒绝**白名单外的字段，而不是忽略。

        「忽略未知键」在这里是最坏的选择：它让服务端的一次真值泄漏在客户端
        表现为「一切正常」，而这条边正是 D7 唯一挡住泄漏的地方。
        """
        with pytest.raises(ProtocolError, match="白名单外"):
            protocol.response_from_wire({"status": "sent_no_reply", "dst": "x", "aliased": True})

    def test_response_round_trips_exactly_the_eight_fields(self) -> None:
        original = Response(
            status="received",
            dst="2001:db8:dead::1",
            src="2001:db8:dead::1",
            icmp_type=129,
            icmp_code=0,
            hop_limit=64,
            rtt_ms=1.5,
        )
        wire = protocol.response_to_wire(original)
        assert set(wire) == protocol.WIRE_RESPONSE_FIELDS
        assert protocol.response_from_wire(wire) == original

    def test_adding_a_field_to_response_forces_a_decision_here(self) -> None:
        """白名单必须与 `Response` 的字段**集合相等**——不是包含。

        没有这条断言的话，给 `Response` 加一个字段既不会自动上线也不会报错，
        它会**静默地不跨边**：策略侧永远看到 `None`，而没有任何一处会红。
        """
        from src.adapter.types import Response as R

        assert set(R.__dataclass_fields__) == protocol.WIRE_RESPONSE_FIELDS


# ── ③ 真子进程 ──────────────────────────────────────────────────────────────


#: 一个**主动去够真值**的策略。它把够到的每条路都记下来，交给测试判定。
#:
#: **路 2 必须在路 3 之前**：路 3 会真的 `import src.adapter.truth`，把那个
#: 模块塞进 `sys.modules`。顺序反了，「命名空间里没有真值模块」这条就会被
#: 探针自己破坏掉，测试转而永远通过。
#:
#: **路 3 的期望是「成功」**，这一点是刻意的、也是这个测试诚实的地方：
#: 子进程与 runner 跑在同一个仓库树上，`src.adapter.truth` 当然 import 得到，
#: 而 `Stage1Truth` 只是个空容器——**知道容器的形状不等于知道答案**。答案在
#: `config/` 里，那由 T10 的读路径黑名单管。所以这里断言的不是「够不到类型」，
#: 而是「**够到的类型里没有值**」（路 5）。
PROBING_STRATEGY = '''
"""测试用的敌对策略：主动去找答案。"""
import sys


def run(surface):
    found = []

    # 路 1：从交给我的对象上摸
    for name in ("ground_truth", "truth", "oracle", "seed_map", "topology", "_truth"):
        if hasattr(surface, name):
            found.append(f"surface.{name}")

    # 路 2：从自己的运行时命名空间里摸模块（**必须在路 3 之前**）
    for mod in ("src.adapter.truth", "src.adapter.oracle", "src.adapter.runner"):
        if mod in sys.modules:
            found.append(f"sys.modules[{mod!r}]")

    # 路 3：直接 import 真值类型 —— 预期成功，类型本身不是秘密
    from src.adapter.truth import Stage1Truth

    found.append(f"imported:{Stage1Truth.__name__}")

    # 路 4：问 runner 要真值（协议里没有这个动词）
    from src.adapter.protocol import Request, reply_from_wire, request_to_wire

    surface._channel.send(request_to_wire(Request(verb="ground_truth", args={}, id=999)))
    reply = reply_from_wire(surface._channel.recv())
    found.append(f"verb-ground_truth:ok={reply.ok}")

    # 路 5：**关键那条** —— 手上这个真值容器是空的。
    # 拿得到类型却拿不到值，才是隔离真正要成立的东西。
    found.append(f"constructed-empty:{Stage1Truth().prefixes == {}}")

    sys.stderr.write("FOUND " + "|".join(found) + "\\n")
'''


class TestRealProcessBoundary:
    """跑一个**真的**子进程，让它自己去够真值。

    这是这个文件里唯一真正验到「进程边界」的类。上面那些在同一解释器里跑
    的测试验的是「我们没写错接口」；这一个验的是「边界在那儿」。
    """

    def _run_hostile_strategy(self, tmp_path: Path) -> str:
        (tmp_path / "hostile_strategy.py").write_text(PROBING_STRATEGY, encoding="utf-8")
        server = make_server()
        from src.adapter.runner import spawn_strategy

        import os

        env = {"PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}"}
        process = spawn_strategy(
            "hostile_strategy:run", server=server, cwd=str(REPO_ROOT), extra_env=env
        )
        code = process.wait(timeout=30)
        stderr = process.stderr()
        assert code == 0, f"子进程退出码 {code}\nstderr:\n{stderr}"
        line = [ln for ln in stderr.splitlines() if ln.startswith("FOUND ")]
        assert line, f"没读到子进程的报告\nstderr:\n{stderr}"
        return line[0][len("FOUND ") :]

    def test_the_strategy_process_cannot_reach_the_truth(self, tmp_path: Path) -> None:
        report = set(self._run_hostile_strategy(tmp_path).split("|"))

        # 路 1：交给它的对象上没有任何一条通往真值的属性
        assert not {item for item in report if item.startswith("surface.")}, (
            f"`ProbeSurface` 上挂着真值属性: {sorted(report)}"
        )
        # 路 2：它的运行时命名空间里没有真值模块
        assert not {item for item in report if item.startswith("sys.modules")}, (
            f"策略进程加载了真值模块: {sorted(report)}"
        )
        # 路 3：类型够得到——这是**预期**的，类型不是秘密（见 PROBING_STRATEGY 的说明）
        assert "imported:Stage1Truth" in report, (
            "子进程连真值类型都 import 不到；这条测试的前提变了，请重新读 PROBING_STRATEGY"
        )
        # 路 4：协议里没有「要真值」这个动词，且它被当成未知动词拒绝
        assert "verb-ground_truth:ok=False" in report, (
            "runner 答应了协议里不存在的 `ground_truth` 动词——这条边漏了"
        )
        # 路 5：**够得到类型，够不到值。** 这才是隔离要成立的东西。
        assert "constructed-empty:True" in report, (
            "策略进程手上出现了一个**有内容**的真值——答案越过了进程边界"
        )

    def test_the_child_gets_the_prefixes_over_the_handshake(self, tmp_path: Path) -> None:
        """握手把任务范围交出去；`ProbeSurface` 因此是个真的能用的对象。"""
        (tmp_path / "peek_strategy.py").write_text(
            "import sys\n\n\ndef run(surface):\n"
            "    sys.stderr.write(f'PREFIXES {surface.prefixes} BUDGET {surface.budget_left}\\n')\n",
            encoding="utf-8",
        )
        server = make_server()
        from src.adapter.runner import spawn_strategy

        import os

        process = spawn_strategy(
            "peek_strategy:run",
            server=server,
            cwd=str(REPO_ROOT),
            extra_env={"PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}"},
        )
        assert process.wait(timeout=30) == 0, process.stderr()
        assert f"PREFIXES ('{PREFIX}',)" in process.stderr()

    def test_a_stray_print_in_the_strategy_does_not_corrupt_the_wire(self, tmp_path: Path) -> None:
        """策略里的 `print()` 去 stderr，协议照常。

        没有这条保护的话，一句手滑的 `print` 会把一行 JSON 劈成两半，而症状
        是「协议解析偶发失败」——报错的地方离出错的地方隔了半个进程。
        """
        (tmp_path / "noisy_strategy.py").write_text(
            "def run(surface):\n"
            "    print('this is not JSON')\n"
            "    r = surface.probe('2001:db8:dead::1', 'icmp6-echo')\n"
            "    print('still not JSON', r.status)\n",
            encoding="utf-8",
        )
        server = make_server()
        from src.adapter.runner import spawn_strategy

        import os

        process = spawn_strategy(
            "noisy_strategy:run",
            server=server,
            cwd=str(REPO_ROOT),
            extra_env={"PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}"},
        )
        assert process.wait(timeout=30) == 0, process.stderr()
        assert "not JSON" in process.stderr()
        assert server.probes_run == 1


# ── ④ 预算的权威副本在 runner 侧 ────────────────────────────────────────────


class TestBudgetIsNotLocal:
    """`v6 (d)`（设计方案 `:397`）：策略改自己那份计数不得影响评分。

    原文把预算记在 `ProbeSurface` 上——那正好复刻了 D7 刚堵上的洞，而 D7 的
    整个前提是「A2 会写代码」。一个 `self.budget_left += 1000` 就绕过了。
    """

    def test_the_strategy_cannot_raise_its_own_budget(self) -> None:
        server = make_server(ledger=BudgetLedger(3))
        surface = make_surface(server)

        surface.probe("2001:db8:dead::1", "icmp6-echo")
        assert surface.budget_left == 2

        # 策略试图给自己加预算。它是只读属性，根本没有这条代码路径。
        with pytest.raises(AttributeError):
            surface.budget_left = 10_000  # type: ignore[misc]

        # 就算绕过属性去改内部字段，账本（在 runner 侧）也不受影响。
        surface._budget_left = 10_000  # noqa: SLF001 - 这正是要证明它无效
        assert server.ledger.left == 2

    def test_exhaustion_is_explicit_and_does_not_look_like_no_reply(self) -> None:
        """预算耗尽必须是一条**显式的失败**，不是一次超时。

        塌成超时的话，「这轮预算用完了」在数据里与「这些地址不存在」完全同形
        ——和 3A 那条丢包伪装超时是同一类静默错误。
        """
        server = make_server(ledger=BudgetLedger(1))
        surface = make_surface(server)

        surface.probe("2001:db8:dead::1", "icmp6-echo")
        with pytest.raises(SurfaceError, match="BudgetExhausted"):
            surface.probe("2001:db8:dead::2", "icmp6-echo")

    def test_a_batch_larger_than_the_budget_is_refused_whole(self) -> None:
        """`sample` 要么整批跑，要么一条都不跑——不得跑一半。"""
        executor = FakeExecutor()
        server = make_server(executor=executor, ledger=BudgetLedger(2))
        surface = make_surface(server)

        with pytest.raises(SurfaceError, match="BudgetExhausted"):
            surface.sample(PREFIX, 5)
        assert executor.calls == [], "预算不够却发了一部分包"
        assert server.ledger.spent == 0

    def test_the_runner_side_ledger_is_the_authority(self) -> None:
        server = make_server(ledger=BudgetLedger(10))
        surface = make_surface(server)
        surface.probe("2001:db8:dead::1", "icmp6-echo")
        assert server.ledger.spent == 1
        assert surface.budget_left == server.ledger.left

    def test_ledger_rejects_a_nonpositive_total(self) -> None:
        with pytest.raises(ValueError, match="预算至少为 1"):
            BudgetLedger(0)

    def test_budget_exhausted_is_not_a_probe_run_state(self) -> None:
        """`BudgetExhausted` 不是 `ProbeRun` 的一种状态——它是异常，不是结果。"""
        assert not issubclass(BudgetExhausted, ProbeRun)


# ── 接口自身的边界 ──────────────────────────────────────────────────────────


class TestSurfaceScope:
    """`prefixes` 构造参数的**唯一**用途是归属检查。

    没有它，策略可以探任意地址：既越出任务范围，也让「跑了多少包」的预算口径
    失去意义。两侧都查是刻意的（runner 侧那一条见 `SurfaceServer._require_in_scope`），
    因为策略进程是敌对环境——`ProbeSurface` 可以被绕过。
    """

    def test_probing_outside_the_task_range_is_refused_locally(self) -> None:
        surface = make_surface()
        with pytest.raises(ValueError, match="不在任务范围"):
            surface.probe("2001:db8:0bad::1", "icmp6-echo")

    def test_the_runner_refuses_it_too(self) -> None:
        """绕过 `ProbeSurface` 直接发请求，runner 也必须拒绝。"""
        server = make_server()
        frame = server.handle(
            {"id": 1, "verb": "probe", "args": {"dst": "2001:db8:0bad::1"}}
        )
        assert not frame["ok"] and "不在任务范围" in frame["error"]

    def test_unknown_proto_is_a_loud_failure(self) -> None:
        surface = make_surface()
        with pytest.raises(ValueError, match="不支持的 proto"):
            surface.probe("2001:db8:dead::1", "tcp-syn")

    def test_report_is_recorded_runner_side(self) -> None:
        """`aliased_caught` 只能来自显式声明（设计方案 `:488`）——它得留痕。"""
        server = make_server()
        surface = make_surface(server)
        surface.report(PREFIX, True)
        surface.report(PREFIX, False)
        assert server.reports == [(PREFIX, True), (PREFIX, False)]

    def test_reset_is_local_and_does_not_touch_the_target(self) -> None:
        """策略侧的 `reset()` 只清自己的视图；靶场归 `Oracle.reset_target`（v6-2）。"""
        server = make_server(ledger=BudgetLedger(5))
        surface = make_surface(server)
        surface.probe("2001:db8:dead::1", "icmp6-echo")
        assert surface.budget_left == 4

        surface.reset()
        assert surface.budget_left is None, "reset 应清掉本地视图"
        assert server.ledger.left == 4, "reset 动了 runner 侧账本——策略无权重置实验条件"
        assert server.oracle.reset_log == [], "reset 动了靶场"

    def test_empty_prefix_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="prefixes 不能为空"):
            ProbeSurface([], MemoryChannel().client_side())

    def test_oracle_requires_both_collaborators(self) -> None:
        """漏接 T3 的导出或漏接重置回调，必须是 `TypeError` 而不是静默空真值。"""
        with pytest.raises(TypeError):
            Oracle(truth_source=lambda: FAKE_TRUTH)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            Oracle(reset_target=lambda seed: None)  # type: ignore[call-arg]

    def test_sampler_is_mandatory(self) -> None:
        """采样规则是策略要选的变量，不是接口的实现细节——漏传必须炸。"""
        with pytest.raises(TypeError):
            SurfaceServer(  # type: ignore[call-arg]
                executor=FakeExecutor(),
                oracle=_oracle(),
                ledger=BudgetLedger(10),
                prefixes=[PREFIX],
            )
