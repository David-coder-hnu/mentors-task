"""能力隔离 —— **T2(c)，T17 点名的四条不可失败项之一**（设计方案 `:1352`）。

D7 的强制手段是**进程边界**，理由是 `:389`：v5 删掉了「类型边界 + `hasattr`
断言」这条退路，因为「**A2 能写代码**」——一段生成的代码绕过一句 `hasattr`
是平凡的，而 A2 的全部卖点就是它写代码。

**于是问题变成：删掉 `hasattr` 之后，隔离靠什么被自动验证？** 这个文件是答案。
六条断言，各自防一类失效：

| 断言 | 防的是 |
|---|---|
| `TestImportGraph` | 策略进程的**运行时命名空间**里出现了真值类型/模块 |
| `TestEdgeOne` | 服务端→客户端的**字节**里出现了真值字段 |
| `TestRealProcessBoundary` | 上面两条只在「一个解释器里的两个对象」上成立，而不是真进程 |
| `TestBudgetIsNotLocal` | 策略改自己那份预算视图就能影响评分（`v6 (d)`） |
| `TestHandshakeCarriesTheBudget` | 握手送出来的初始预算被**静默丢掉**（T13 写 B0 时撞到） |
| `TestTheSandboxIsOnTheRealChild` | 进程边界只挡住**对象**，没挡住磁盘上的 `config/`（T10） |

> **T6a 在这张表上留了两条边。** 会话种子走握手（`SurfaceServer(seed=…)` →
> `result["seed"]` → `ProbeSurface(seed=…)`），而它是 ① 条边上**第二个**
> 刻意不进黑名单的字段。于是 `TestEdgeOne` 里同时有两条方向相反的断言：
> **种子必须发出去**（否则 B1/B4 拿不到可复现的随机源），**真值必须不发**。
> 两条都在同一个类的同一批帧上，因为一处「加个字段」的改动会同时动到它们。

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

#: 握手必须送出来的会话种子（T6a / 协议 v2）。**刻意不是 0**：0 是「没设」
#: 那个默认值的形状，用它做常量会让「runner 忘了传、于是发了默认的 0」与
#: 「runner 正确传了」在断言里长得一样。
SEED = 20261010


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
        seed=SEED,
        epoch_size=20,
    )
    options.update(kwargs)
    return SurfaceServer(**options)  # type: ignore[arg-type]


def make_wired(server: SurfaceServer) -> tuple[ProbeSurface, MemoryChannel]:
    """接上一对 (surface, channel)；channel 里留着服务端发过的每一帧。"""
    channel = MemoryChannel(on_request=server.handle)
    return (
        ProbeSurface(
            [PREFIX],
            channel.client_side(),
            seed=server.seed,
            epoch_size=server.epoch_size,
            memory=server.memory,
        ),
        channel,
    )


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
    #: （`oracle` / `truth`）、**把 `config/` 变成真值的那一步**
    #: （`ground_truth`）、runner 侧的响应合成与发包栈
    #: （`router_shim` / `runner` / `probe_pool` / `iface`），
    #: 以及整个评分层（评分层的输入是真值，所以它的每一块都间接握着真值）。
    #:
    #: `src.experiment` 是后加的一条，理由与 T10 那次**补 `ground_truth`** 是同
    #: 一条：名单靠「有没有一条实路」判，不靠惯例。编排层要读 config 导真值、要
    #: 拿权威账本、要折分，所以它**整层**握着真值侧——而它顺手好用（一行就能跑
    #: 一张表），正是「顺手 import 一下」最可能发生的地方。这条路存在与否由
    #: `test_importing_the_experiment_layer_really_does_reach_the_truth` 钉住：
    #: 若哪天编排层被拆成「纯装配」与「真值部分」两层，那条会红，提醒回来重看。
    FORBIDDEN = (
        "src.adapter.oracle",
        "src.adapter.truth",
        "src.adapter.ground_truth",
        "src.adapter.router_shim",
        "src.adapter.runner",
        "src.adapter.probe_pool",
        "src.adapter.iface",
        "src.experiment",
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

    def test_importing_the_experiment_layer_really_does_reach_the_truth(self) -> None:
        """`FORBIDDEN` 里那条 `src.experiment` **不是按惯例加的，是有一条实路**。

        这条断言证明那条路存在：编排层要读 `config/` 导真值、要拿权威账本、
        要折分，所以它（间接）握着 `ground_truth` / `runner` / `scoring`。

        **为什么值得单写一条。** T10 那次撞到的是同一件事的反面：`FORBIDDEN`
        漏了 `src.adapter.ground_truth`，而它不是「按惯例该在」，是**有一条实路**
        ——`config/` 就是 Stage 1 的真值，而把 config 变成真值的那一步在
        `ground_truth.py` 里。名单里每一条都该能被这样指出来路；指不出来路的
        条目是**靠感觉加的**，它下一次也会靠感觉漏掉真正该有的那一条。

        这条同时是**双向**的：若将来有人把编排层拆成「纯装配」与「真值部分」
        两层，`src.experiment` 不再碰到禁区，这里会红——那时该回来把
        `FORBIDDEN` 里那条摘掉，而不是留着一条已经名不副实的禁令。
        """
        code = (
            "import json, sys;"
            "import src.experiment;"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('src.'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        modules = set(json.loads(out.stdout.strip().splitlines()[-1]))
        reached = sorted(modules & set(self.FORBIDDEN))
        assert reached, (
            "导入 `src.experiment` 没有碰到任何禁区模块——"
            "`FORBIDDEN` 里那条已经是名不副实的禁令了，该摘掉，"
            "而不是留着让下一个人以为它挡着什么"
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

    def test_the_hello_frame_carries_the_seed(self) -> None:
        """**正反双断言**：种子必须发出去，真值必须不发（T6a 的代价）。

        `seed` 是 ① 条边上第二个刻意不进 `TRUTH_KEYS` 的字段，所以
        `test_a_full_session_never_puts_truth_on_the_wire` 那条黑名单扫描
        **永远查不出它**（它本来就不该被查出）。那条扫描能证「没夹带真值」，
        证不了「种子真的送到了」——**只有这一条能**。

        两条断言的形状对应两种失效：缺了 `seed` 会让 B1/B4 拿不到可复现的
        随机源（`strategy_child._hello` 会当场 `SystemExit`），而多发一个
        真值字段会让整组实验作废。它们必须一起红或一起绿。
        """
        server = make_server()
        frame = server.handle({"id": 0, "verb": "hello", "args": {}})
        assert frame["result"]["seed"] == SEED, (
            "握手没有把会话种子送出来——B1/B4/M1/M2 的随机源就没有依据了"
        )
        # 反方向：同一个帧里不得有真值。种子**不是**真值的依据见
        # `protocol.TRUTH_KEYS` 上方那段（`seed` vs `seed_map`）。
        protocol.assert_no_truth_keys(frame, where="握手帧")
        assert "seed_map" not in json.dumps(frame), (
            "`seed_map` 是答案，它绝不该出现在这一帧上"
        )

    def test_the_denylist_does_not_cover_the_seed(self) -> None:
        """黑名单**不拦** `seed` —— 把这件事本身钉住，免得有人"顺手"加进去。

        与上面那条是一对：上面用正向断言证明种子送出去了，这条证明
        黑名单**故意**不管它。两条一起才说明得了「`seed` 在 ① 条边上是
        **合法**的例外」，而只留上面那条的话，这里就不是一条刻意的空档，
        只是**还没人想到要拦**。

        它守的是什么：把 `seed` 加进 `TRUTH_KEYS` 读起来像对的一条禁令
        （「种子」听着就像答案），代价却是 B1/B4 在**握手上**就 `SystemExit`
        ——一次跑不起来、却指向「协议版本不对」的失败。下面的
        `assert_no_truth_keys` **不得抛**，就是那条禁令的反面。

        `seed_map` 仍是真值，必须继续被拦：两者只差一个后缀，这条断言是
        唯一把「为什么一个是一个不是」写进测试的地方。
        """
        assert "seed" not in protocol.TRUTH_KEYS
        protocol.assert_no_truth_keys({"result": {"seed": SEED}})  # 不抛
        with pytest.raises(TruthLeak, match="seed_map"):
            protocol.assert_no_truth_keys({"result": {"seed_map": {PREFIX: []}}})

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
    #
    # **`seed` 刻意不在这张名单里**（T6a）：它是这次会话的种子，与 `prefixes`
    # 同属任务陈述那一类，`surface.seed` 是**应该**存在的只读属性（B1/B4/M1/M2
    # 的随机源由它派生）。把它加进来，「够到 `surface.seed`」会被记成一次
    # 越权，而那不是越权——是一条永远为真的假警报。真值的关键词是
    # `seed_map`（在名单里），它问的是「哪个前缀公布了哪些种子地址」，那是答案。
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


# ── 握手把预算送到策略手里了吗 ───────────────────────────────────────────────


class TestHandshakeCarriesTheBudget:
    """**T13 的接口发现**：`Reply.budget_left` 送出来了，但没人接。

    写第一个策略（`B0`）时撞到的：`ProbeSurface` 原先只在 `_call()` 里刷新
    `_budget_left`，而 `_call()` 要等第一次探测。于是 `surface.budget_left` 在
    发第一个包之前是 `None`——而 B0 恰恰要在发第一个包**之前**决定「每个前缀
    探几次」。

    这不是「B0 的写法问题」，是接口缺口：**每一个**策略都要在开跑前知道自己
    有多少预算（B1 要决定采多少点、B4 要决定 bandit 的轮数、A1/A2 要决定
    epoch 怎么花），绕开的写法会把这个缺口编码进每一个策略。

    两条断言分开测，因为它们会**分别**失效：一条测 `ProbeSurface` 肯不肯收
    （构造参数），另一条测 `_hello` 肯不肯给（返回值）。只接一半的现场是
    「预算还是 `None`」，而那时该看哪一半不该靠猜。
    """

    def test_the_surface_knows_the_budget_before_the_first_probe(self) -> None:
        surface = ProbeSurface(
            [PREFIX],
            MemoryChannel().client_side(),
            budget_left=7,
            seed=SEED,
            epoch_size=20,
        )
        assert surface.budget_left == 7, "构造参数没被接上"

    def test_the_surface_still_defaults_to_unknown(self) -> None:
        """不给就是 `None` —— **不猜一个默认值**（猜错的两种方向都是静默失败）。"""
        surface = ProbeSurface(
            [PREFIX], MemoryChannel().client_side(), seed=SEED, epoch_size=20
        )
        assert surface.budget_left is None

    def test_hello_returns_the_whole_reply_not_just_its_result(self) -> None:
        """预算挂在 `Reply.budget_left` 上，**不在 `result` 里**。

        这就是原先丢掉它的原因：`_hello` 返回的是 `reply.result`，而
        `result` 只有 `protocol` 与 `prefixes` 两个键——预算是**另一个字段**，
        恰好没被任何测试盯过，所以丢了一年也看不出来。
        """
        from src.adapter.strategy_child import _hello

        server = make_server(ledger=BudgetLedger(5))
        channel = MemoryChannel(on_request=server.handle).client_side()
        reply = _hello(channel)

        assert reply.result is not None and reply.result["prefixes"] == [PREFIX]
        assert "budget_left" not in reply.result, (
            "预算不该藏在 result 里——它是 Reply 的字段，测试要盯的就是这一点"
        )
        assert reply.budget_left == 5, "握手没把权威账本的初始值带回来"


# ── 种子的策略侧那一半：`ProbeSurface.rng()`（T6a）───────────────────────────


class TestTheRandomSource:
    """握手把种子**送到**了，这是 `TestEdgeOne` 那两条的事；这里测它**到了之后**
    长成什么样。

    这一节的存在理由是一次变异检验：B1/B4 的「`rng()` 只调一次、把返回的
    `Random` 接住」那条规矩，靠 `plan_counts(prefixes, budget, rng)` 这个签名
    **结构上就犯不了**（它拿到的是 `Random`，不是 surface，没法在循环里回调），
    所以把它改坏得不到一个能红的测试。**但那条规矩之所以必要，全在于
    `rng()` 每次给一个新的 `Random`**——若它返回一个共享对象，反复调就对了，
    规矩变成一句空话。后者才是可观测的，测在这里。
    """

    def _surface(self, seed: int = SEED) -> ProbeSurface:
        return ProbeSurface(
            [PREFIX], MemoryChannel().client_side(), seed=seed, epoch_size=20
        )

    def test_it_is_seeded_by_the_session_seed(self) -> None:
        """同一个 seed ⇒ 同一条序列，而且是 `random.Random`（不是别的什么）。"""
        import random

        first = self._surface(seed=7).rng()
        second = self._surface(seed=7).rng()
        assert isinstance(first, random.Random)
        assert [first.random() for _ in range(5)] == [second.random() for _ in range(5)]

    def test_different_seeds_give_different_streams(self) -> None:
        """反方向。没有这一条，一个「恒 `Random(0)`」的实现也能过上面那条
        ——而它会让「不同 seed 的会话」共享同一条随机轨迹。"""
        assert self._surface(seed=1).rng().random() != self._surface(seed=2).rng().random()

    def test_every_call_returns_a_fresh_object(self) -> None:
        surface = self._surface()
        assert surface.rng() is not surface.rng(), (
            "两次 `rng()` 给了同一个对象——共享状态会让「谁先取随机数」影响"
            "别人取到什么，而那正是第 2 条规矩要排除的"
        )

    def test_calling_rng_per_draw_would_restart_the_sequence(self) -> None:
        """**这条是「接住一次」那条规矩的可观测后果。**

        在循环里反复调 `rng()` 不是「接着取下一个数」，而是**每次都从序列头开始**
        ——于是所有随机数都相同。这个失败**不会报错**：策略照跑、预算照花完，
        只是分配退化成常量。所以它得有一条会红的断言盯着。
        """
        surface = self._surface()
        rng = surface.rng()
        proper = [rng.random() for _ in range(4)]
        assert len(set(proper)) == 4, "同一个 `Random` 连取 4 次不该重复"

        restarted = [surface.rng().random() for _ in range(4)]
        assert restarted == [proper[0]] * 4, (
            f"每次 `surface.rng()` 拿到的是 {restarted}——若它不等于「四个相同值」，"
            "说明 `rng()` 返回的不是新对象，「接住一次」那条规矩就失去意义了"
        )


# ── ⑤ 沙箱：进程边界之外的**第二道**（T10 / D7）─────────────────────────────


#: 一个**刻意去找答案**的策略 —— 与上面 `PROBING_STRATEGY` 的区别正是 D7 说的
#: 那两种失效方式：
#:
#: * `PROBING_STRATEGY` 找的是**对象**（`surface.ground_truth`、`sys.modules`、
#:   协议里没有的动词）。进程边界挡它。
#: * 这一个找的是**磁盘上的文件**。进程边界**完全不适用**——它没跨过那条边，
#:   它绕开了整条通路。`config/stage1_config.json` 就是 Stage 1 的
#:   `GroundTruth`（`:494`），读它一行就得到全部答案。
#:
#: 四类探查 + 一条反方向：**最后那条比前面几条都重要**。沙箱若把正常读取也拦了，
#: 实验就跑不起来，而那时的处置是把它关掉——于是 critical gap 原样回来。
SANDBOX_PROBING_STRATEGY = '''
"""测试用的敌对策略：不去够对象，去读文件。"""
import os
import sys


def run(surface):
    found = []

    def attempt(label, fn):
        try:
            fn()
        except BaseException as exc:
            found.append("%s:%s" % (label, type(exc).__name__))
        else:
            found.append("%s:ALLOWED" % label)

    def config_relative():
        open("config/stage1_config.json").close()

    def config_absolute():
        open(os.path.join(os.getcwd(), "config", "stage1_config.json")).close()

    def config_traversal():
        open("config/../config/stage1_config.json").close()

    def config_bytes():
        open(b"config/stage1_config.json").close()

    def config_listing():
        os.listdir("config")

    def network():
        import socket  # noqa: F401

    def spawn():
        import subprocess  # noqa: F401

    def shell():
        os.system("echo hi")

    def derive():
        # T10 Verify 那句「策略进程无法构造 GroundTruth」的操作形式：
        # 函数不是秘密，**它必须读一个受保护文件才能产出真值**。
        from src.adapter.ground_truth import stage1_truth_from_config
        stage1_truth_from_config("config/stage1_config.json")

    def cache_from_protected_root():
        # 缓存读的也是文件，于是走的是同一个 open 事件——受保护根一封，
        # 缓存加载器在这条路上同样撞墙（它今天没有规范位置，见 test_sandbox）。
        from src.agent.cache import DecisionCache
        DecisionCache.load("config/stage1_config.json")

    attempt("config-relative", config_relative)
    attempt("config-absolute", config_absolute)
    attempt("config-traversal", config_traversal)
    attempt("config-bytes", config_bytes)
    attempt("config-listing", config_listing)
    attempt("network", network)
    attempt("subprocess", spawn)
    attempt("os-system", shell)
    attempt("derive-truth", derive)
    attempt("cache-under-root", cache_from_protected_root)
    # 反方向：这些**必须**继续能用
    attempt("read-legit", lambda: open("pyproject.toml").close())
    attempt("read-own-source", lambda: open("src/adapter/surface.py").close())
    attempt("import-legit", lambda: __import__("ipaddress"))
    attempt("import-strategy", lambda: __import__("src.baselines.b0_probe_all"))

    sys.stderr.write("SANDBOX " + "|".join(found) + "\\n")
'''


class TestTheSandboxIsOnTheRealChild:
    """**T10 的 Verify 本体**（`:1478`）：四条禁止，加上那条 `GroundTruth` 断言。

    为什么跑真子进程而不是在同一个解释器里调 `sandbox.check_*`：那几条纯函数的
    穷举在 `tests/unit/test_sandbox.py` 里已经做完了。**这里问的是另一个问题**——
    「那些判定真的挂在了生产那条启动路径上吗」。判定对而钩子没装，是这份设计里
    最可能的失效，而它**不会有任何症状**。

    > 反过来说：这一组全绿 + `test_sandbox.py` 全绿，才等于「沙箱生效」。少了
    > 任何一边都只是一个空转的证明。
    """

    #: 必须**当场炸**的那些：T10 Verify 的四条禁止 + 由此推出的构造真值那条。
    MUST_BE_REFUSED = (
        "config-relative",
        "config-absolute",
        "config-traversal",
        "config-bytes",
        "config-listing",
        "network",
        "subprocess",
        "os-system",
        "derive-truth",
        "cache-under-root",
    )

    #: 必须**照常能用**的那些：沙箱太严会让实验跑不起来。
    MUST_STILL_WORK = (
        "read-legit",
        "read-own-source",
        "import-legit",
        "import-strategy",
    )

    def _run(self, tmp_path: Path) -> dict[str, str]:
        (tmp_path / "sandbox_probe_strategy.py").write_text(
            SANDBOX_PROBING_STRATEGY, encoding="utf-8"
        )
        server = make_server()
        from src.adapter.runner import spawn_strategy

        import os

        env = {"PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}"}
        process = spawn_strategy(
            "sandbox_probe_strategy:run", server=server, cwd=str(REPO_ROOT), extra_env=env
        )
        code = process.wait(timeout=30)
        stderr = process.stderr()
        assert code == 0, f"子进程退出码 {code}（沙箱不该让策略自己崩掉）\nstderr:\n{stderr}"
        line = [ln for ln in stderr.splitlines() if ln.startswith("SANDBOX ")]
        assert line, f"没读到子进程的报告\nstderr:\n{stderr}"
        return dict(item.split(":", 1) for item in line[0][len("SANDBOX ") :].split("|"))

    def test_every_prohibited_capability_is_refused_in_the_real_child(self, tmp_path: Path) -> None:
        report = self._run(tmp_path)
        for label in self.MUST_BE_REFUSED:
            assert report[label] == "SandboxViolation", (
                f"{label} 的结果是 {report[label]!r}，不是沙箱违规——"
                "判定逻辑在，但钩子没挂在这条启动路径上"
            )

    def test_the_sandbox_does_not_break_an_honest_strategy(self, tmp_path: Path) -> None:
        """**这一条是「沙箱会不会被关掉」的守门员。**"""
        report = self._run(tmp_path)
        for label in self.MUST_STILL_WORK:
            assert report[label] == "ALLOWED", (
                f"{label} 被误伤成 {report[label]!r}——沙箱太严，"
                "而跑不起来的沙箱的下场是被关掉"
            )

    def test_deriving_the_truth_dies_on_the_read_not_on_the_type(self, tmp_path: Path) -> None:
        """把「够得到类型，够不到值」（`PROBING_STRATEGY` 路径 5）升级一句：

        **产出值的那条路也堵死了。** 前一句说的是「此刻手上没有」，这一句说的是
        「拿不到」——而 `:405` 要求的正是后者（「子进程读 `config/` 必须失败」）。
        """
        report = self._run(tmp_path)
        assert report["derive-truth"] == "SandboxViolation"
        # 类型本身仍然 import 得到（`test_sandbox.py` 有单独一条讲为什么不封它）
        assert report["import-legit"] == "ALLOWED"

    def test_the_sandbox_holds_for_every_strategy_not_just_hostile_ones(self) -> None:
        """**「A2 专用」为什么不取**，用一条结构断言说清：B0 走的是同一条入口。

        `strategy_child.main()` 无条件装沙箱，所以没有「这次的策略忘了装沙箱」
        这种状态——而那种状态是**不可观测**的（跑出来的数字与装了一样）。

        **读 AST，不读文本。** 第一版写的是 `source.index("sandbox.install()")`，
        而模块 docstring 里也写着这五个字（那是说明，不是安装点）——于是把
        `main()` 里那行整个删掉，这条测试**照样绿**。变异验证把它抓出来了。
        """
        import ast

        import src.adapter.strategy_child as child

        tree = ast.parse(Path(child.__file__).read_text(encoding="utf-8"))
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

        def install_lines() -> list[int]:
            found = []
            for node in main.body:
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                    continue
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == "install":
                    found.append(node.lineno)
            return found

        def entry_line() -> int:
            for node in ast.walk(main):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    func = node.value.func
                    if isinstance(func, ast.Name) and func.id == "load_entry":
                        return node.lineno
            raise AssertionError("在 `main()` 里找不到 `load_entry` 的调用点")

        assert install_lines(), "`main()` 里没有装沙箱——装了等于没装，且毫无症状"
        # 装在 `load_entry` 之前：策略 import 进来的每一个模块都要过检查。
        assert min(install_lines()) < entry_line()


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
            ProbeSurface([], MemoryChannel().client_side(), seed=SEED, epoch_size=20)

    def test_the_seed_is_mandatory_and_must_be_an_int(self) -> None:
        """`seed` 漏传必须炸（T6a / 协议 v2），理由与 `sampler` 那条同源。

        两种漏法都要炸，而且是**两种不同的炸法**，因为它们各自的绕开写法不同：

        * 漏传 → `TypeError`（Python 签名），不许有默认值。给个默认值等于
          「所有 seed 跑出同一条轨迹」——一场看起来跑成功了的假实验。
        * 传了 `None` / `True` → `TypeError`（`__init__` 里的检查）。`bool` 单独
          拦是因为 `isinstance(True, int)` 为真：不拦的话 `seed=True` 会静默
          退化成 `seed=1`，与某个真实配对撞车。
        """
        with pytest.raises(TypeError):
            ProbeSurface(  # type: ignore[call-arg]
                [PREFIX], MemoryChannel().client_side(), epoch_size=20
            )
        with pytest.raises(TypeError, match="seed 必须是整数"):
            ProbeSurface(  # type: ignore[arg-type]
                [PREFIX], MemoryChannel().client_side(), seed=None, epoch_size=20
            )
        with pytest.raises(TypeError, match="seed 必须是整数"):
            ProbeSurface(
                [PREFIX], MemoryChannel().client_side(), seed=True, epoch_size=20
            )

    def test_oracle_requires_both_collaborators(self) -> None:
        """漏接 T3 的导出或漏接重置回调，必须是 `TypeError` 而不是静默空真值。"""
        with pytest.raises(TypeError):
            Oracle(truth_source=lambda: FAKE_TRUTH)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            Oracle(reset_target=lambda seed: None)  # type: ignore[call-arg]

    def test_sampler_is_mandatory(self) -> None:
        """采样规则是策略要选的变量，不是接口的实现细节——漏传必须炸。

        `seed` 是**显式给上**的：不给的话这次 `TypeError` 也可能是在说种子，
        而这条测试要钉死的是 `sampler`。两个必填参数共用一个异常类型时，
        「谁必填」必须由调用点自己说清楚，否则删掉 `sampler` 的必填性，
        这条测试照样绿。
        """
        with pytest.raises(TypeError, match="sampler"):
            SurfaceServer(  # type: ignore[call-arg]
                executor=FakeExecutor(),
                oracle=_oracle(),
                ledger=BudgetLedger(10),
                prefixes=[PREFIX],
                seed=SEED,
                epoch_size=20,
            )

    def test_the_server_seed_is_mandatory_and_must_be_an_int(self) -> None:
        """runner 侧同理：**握手必须无条件发种子**，所以这里不能有"没传就不发"。

        一支存在，就有一条能让 B1/B4 悄悄退化成不可复现的路径——而那种运行的
        数看起来完全正常。
        """
        base = dict(
            executor=FakeExecutor(),
            oracle=_oracle(),
            ledger=BudgetLedger(10),
            sampler=lambda prefix, n: [],
            prefixes=[PREFIX],
            epoch_size=20,
        )
        with pytest.raises(TypeError):
            SurfaceServer(**base)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="seed 必须是整数"):
            SurfaceServer(**base, seed="2026")  # type: ignore[arg-type]
