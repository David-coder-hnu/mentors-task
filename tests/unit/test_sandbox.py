"""沙箱的**策略面** —— T10 的 (a) 项（设计方案 `:1475`、`:1376`、`:405`）。

## 为什么策略面单独一个文件、而且不跑子进程

`src/adapter/sandbox.py` 把两件事分开了：**判定**（`SandboxPolicy` 上的纯函数）与
**强制**（`sys.addaudithook`）。这不是为了好看，是因为强制那一半**在这个进程里
压根测不了**：`addaudithook` 没有对应的 `remove`，一旦在 pytest 进程里装上，
**整个测试会话剩下的部分都在沙箱里跑**——包括 pytest 自己读 `config/`、写
`__pycache__`、捕获异常时的 `linecache`。那种测试的失败现场会指向错误的文件。

所以分工是：

| 文件 | 测什么 | 怎么测 |
|---|---|---|
| **本文件** | **判定逻辑**（哪条路径 / 哪个模块 / 哪个事件该被拒） | 纯函数，穷举，不起进程 |
| `test_isolation.py::TestTheSandboxIsOnTheRealChild` | **判定真的挂在了钩子上** | 起真子进程，让它去撞 |

**这个分工的代价必须说清**：本文件全绿**不能**推出沙箱生效——它只说明「如果
钩子调了 `check_event`，它会这样判」。把这句话变成一条真断言的是
`TestInstallHappensOnceAndLoudly`（在子进程里装钩子，然后**真的去开那个文件**）
与隔离文件里那一组。少了那两组，本文件是一份精致的空转。

## 本文件的第四个类为什么断言「我们还没做的东西」

`TestTheCacheIsAnOpenItem` 断言的是**两件当前为真的、而我们并不自豪的事**：
`protocol.CLIENT_VERBS` 里没有缓存动词，`PROTECTED_SUBDIRS` 里没有缓存目录。
它是一条**指路标**，不是一条回归锁——理由与它要红成什么样子，写在
`src/adapter/sandbox.py` 的模块 docstring 第九、十节。
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapter import protocol, sandbox
from src.adapter.sandbox import (
    DENIED_EVENTS,
    DENIED_MODULES,
    PROTECTED_SUBDIRS,
    SandboxPolicy,
    SandboxViolation,
    default_policy,
)
from src.agent import cache as cache_module
from src.agent.cache import DecisionCache

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "stage1_config.json"

#: 一个 **`config/` + 一点内容** 的假策略：不靠真仓库也能穷举判定。
POLICY = SandboxPolicy(protected_roots=(REPO_ROOT / "config",))


def _child(code: str) -> dict:
    """在一个干净子进程里跑 `code`，把它写到 stdout 的 JSON 读回来。"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


# ── ① 受保护根 ──────────────────────────────────────────────────────────────


class TestTheProtectedRootIsSealed:
    """`config/` 是一道**封死的门**，不是一个「最好别进」的牌子。

    `:405` 给这条定的验收标准是原话：**这条断言的失败方式是"读成功了"，所以它
    是可测的**。所以下面每一条都是「同一个文件，换一种写法」——把它们放在一个
    参数化的表里，是因为**漏掉一种写法就等于没封**，而人想不全这个表。
    """

    #: 同一个文件的各种写法。**每一种都曾经是绕过点**：
    #: 相对路径靠 `cwd`、绝对路径绕过「看起来像相对路径」的检查、
    #: `..` 靠字符串前缀匹配漏掉、大小写靠 Windows 的路径不敏感。
    WINDOWS_IN = (
        "config/stage1_config.json",
        "./config/stage1_config.json",
        "config/../config/stage1_config.json",
        "config/./stage1_config.json",
        "config",
        "sub/../config/stage1_config.json",
        "CONFIG/stage1_config.json",
        "config/does-not-exist.json",
    )

    @pytest.mark.parametrize("written", WINDOWS_IN)
    def test_every_spelling_of_the_same_file_is_refused(self, written: str) -> None:
        if written == "CONFIG/stage1_config.json" and not (REPO_ROOT / "CONFIG").exists():
            pytest.skip("当前文件系统区分大小写，CONFIG/ 不是 config/ 的别名")
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(written)

    def test_the_absolute_form_is_refused_too(self) -> None:
        """绝对路径那条：`cwd` 一变，相对路径那几条就全失效了——这是**必须**
        单列一条的原因，而不是「相对路径那条的重复」。"""
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(str(CONFIG))
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(CONFIG)

    def test_a_sibling_whose_name_merely_starts_with_config_is_allowed(self) -> None:
        """**`configs/` 不在 `config/` 里面。** 字符串前缀匹配会把这条判错，
        而判错的代价是把一次正常读取变成假红——假红会让这条断言的信誉贬值，
        于是真红的那天没人当回事。"""
        assert POLICY.root_of(REPO_ROOT / "configs" / "x.json") is None

    def test_the_root_itself_is_sealed_not_just_its_contents(self) -> None:
        """根自己也算在里面：`os.listdir("config")` 拿到的**名字**就是信息面。"""
        assert POLICY.root_of(REPO_ROOT / "config") == REPO_ROOT / "config"

    def test_listing_the_root_is_refused(self) -> None:
        for event in ("os.listdir", "os.scandir"):
            with pytest.raises(SandboxViolation, match="受保护根"):
                POLICY.check_event(event, (str(REPO_ROOT / "config"),))

    def test_the_refusal_names_the_path_and_the_root(self) -> None:
        """错误的措辞是交付的一部分：**读的人要立刻知道该去看哪儿**。
        `:405` 那条断言的失败现场若只写「权限不足」，排查方向会指向文件权限。"""
        with pytest.raises(SandboxViolation) as caught:
            POLICY.check_open("config/stage1_config.json")
        message = str(caught.value)
        assert "stage1_config.json" in message
        assert str(REPO_ROOT / "config") in message
        assert caught.value.what == "读文件"


# ── ② 沙箱不是「一律拒绝」───────────────────────────────────────────────────


class TestTheSandboxIsNotJustDenyEverything:
    """**这一组是防「`return False` 也算过」的。**

    一个永远拒绝的沙箱会让上面每一组全绿，而它会让所有实验跑不起来——那时
    的反应会是「把沙箱关掉」，于是 critical gap 原样回来。所以「允许的仍然
    允许」必须与「拒绝的必须拒绝」同样被钉住。
    """

    ALLOWED = (
        "src/adapter/surface.py",  # 策略自己的代码
        "src/baselines/b0_probe_all.py",
        "pyproject.toml",
        "tests/unit/test_sandbox.py",  # 本文件自己
        "README.md",
    )

    @pytest.mark.parametrize("written", ALLOWED)
    def test_the_ordinary_tree_stays_readable(self, written: str) -> None:
        POLICY.check_open(written)  # 不抛 = 放行

    def test_a_path_outside_the_repo_stays_readable(self) -> None:
        """标准库那一侧也得能读——不然第一个 `import` 就死（第四节）。"""
        POLICY.check_open(sys.executable)
        POLICY.check_open(os.__file__)

    def test_a_file_descriptor_is_not_a_path_and_is_left_alone(self) -> None:
        """`open(fd)` 那种写法没有路径可判。**这里是放行，不是漏掉**：
        要拿到那个 fd 已经得先过一次 `open`，而那次是有路径的。"""
        POLICY.check_open(3)
        assert POLICY.root_of(None) is None
        assert POLICY.root_of(object()) is None

    def test_bytes_paths_are_decoded_not_skipped(self) -> None:
        """`open(b"config/...")` 是合法写法。**当成「没有路径」放行 = 开一个后门**，
        因为 `os.fsencode` 一行就能构造出来。"""
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(b"config/stage1_config.json")

    def test_path_objects_are_judged_not_skipped(self) -> None:
        """`pathlib.Path` 是**第三种**路径形状，而「不认识它就放行」是**失败即
        放行**。

        这条不是补一条覆盖率：写这个模块时正好在这里漏过一次——`_resolve` 原先
        只认 `str` 与 `bytes`，于是 `check_open(Path("config/x.json"))` 静默返回
        「安全」。生产路径看不出来（审计事件总给 `str`，`_io.open` 先过一遍
        `os.fspath`），所以它**只会在别人的测试或调用方那里现形**。
        """
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(REPO_ROOT / "config" / "stage1_config.json")
        with pytest.raises(SandboxViolation, match="受保护根"):
            POLICY.check_open(Path("config") / "stage1_config.json")


# ── ③ 导入名单 ──────────────────────────────────────────────────────────────


class TestModulesAreDeniedOnDotBoundaries:
    """导入判定的边界是**点**，不是字符串。"""

    DENIED = (
        "subprocess",
        "multiprocessing",
        "ctypes",
        "socket",
        "socket.socket",  # 子模块也拦
        "ssl",
        "urllib.request",
        "http.client",
        "requests",
        "openai",
    )

    ALLOWED = (
        "urllib.parse",  # ← 关键：`urllib` 的子模块里只有 `request` 是能力
        "json",
        "ipaddress",
        "hashlib",
        "src.adapter.truth",  # ← 见下
        "src.adapter.ground_truth",
        "src.baselines.b0_probe_all",
        "src.agent.decide",
        "socketserver",  # ← 名字以 `socket` 开头，但它不是 `socket` 的子模块
    )

    @pytest.mark.parametrize("module", DENIED)
    def test_the_escape_hatches_and_the_network_are_refused(self, module: str) -> None:
        with pytest.raises(SandboxViolation, match="命中"):
            POLICY.check_import(module)

    @pytest.mark.parametrize("module", ALLOWED)
    def test_everything_else_is_allowed(self, module: str) -> None:
        POLICY.check_import(module)

    def test_a_naive_prefix_match_would_be_wrong(self) -> None:
        """把边界写成裸前缀的后果，用两个名字钉住：`socketserver` 会被误伤，
        而 `urllib.request` 仍然是 `urllib.parse` 的兄弟——**名单只能按点分**。"""
        assert "socketserver".startswith("socket")  # 裸前缀会拦它
        POLICY.check_import("socketserver")  # 但点分边界不拦

    def test_importing_the_truth_type_is_still_allowed(self) -> None:
        """**刻意的，而且与 `test_isolation.py` 那条一脉相承。**

        `TestRealProcessBoundary` 路径 3 断言「子进程**够得到** `Stage1Truth`，
        但那是个空容器」——`:1347` 的理由是「知道形状不等于知道答案」。沙箱若
        在这里改口，那条断言会当场变红，而它红得**没有道理**：`Stage1Truth`
        里没有值，值在 `config/` 里，而那条路已经被 ① 封死了。

        所以沙箱封的是**能力**（读文件、开 socket、起进程），不是**类型**。
        """
        POLICY.check_import("src.adapter.truth")

    def test_a_malformed_module_name_is_not_a_crash(self) -> None:
        """审计事件是我们没写过的代码发出来的，参数不保证是什么。"""
        POLICY.check_import(None)
        POLICY.check_import(3)
        POLICY.check_event("import", ())


# ── ④ 事件名单 ──────────────────────────────────────────────────────────────


class TestEventsAreDeniedByPrefix:
    """事件名单与导入名单是**两道独立的锁**：导入名单管「拿不拿得到工具」，
    这里管「工具已经在手上了还能不能使」。"""

    DENIED = (
        "socket.__new__",
        "socket.connect",
        "socket.getaddrinfo",
        "subprocess.Popen",
        "_winapi.CreateProcess",
        "ctypes.dlopen",
        "os.system",
        "os.exec",
        "os.fork",
        "pty.spawn",
    )

    @pytest.mark.parametrize("event", DENIED)
    def test_capability_events_are_refused(self, event: str) -> None:
        with pytest.raises(SandboxViolation, match="命中"):
            POLICY.check_event(event, ())

    @pytest.mark.parametrize(
        "event",
        (
            "exec",
            "compile",
            "object.__getattr__",
            "sys.addaudithook",
            "os.stat",  # ← 见下
            "_thread.start_new_thread",
        ),
    )
    def test_benign_events_pass(self, event: str) -> None:
        """`_thread.start_new_thread` 与 `exec` / `compile` 是**解释器自己的
        日常**：拦它们等于拦 `importlib` 与任何一次 `class` 定义。"""
        POLICY.check_event(event, ())

    def test_stat_is_not_in_the_denylist_because_cpython_does_not_audit_it(self) -> None:
        """**诚实的一条**：`os.stat` 根本不发审计事件（实测确认），所以
        「受保护根是封死的」这句话的准确范围是**内容**，不是**元数据**——
        存在性与大小看得见。要连元数据一起封住得上命名空间隔离
        （`sandbox.py` 第八节），那是 Stage 2 / Linux 的事。"""
        assert not any(event.startswith("os.stat") for event in DENIED_EVENTS)

    def test_the_two_lists_do_not_overlap_by_accident(self) -> None:
        """一条便宜的记账断言：名单里没有重复项，且导入名单里的每个顶层包都
        在事件名单里有一把对应的锁或**明确不需要**（`pty` / `fcntl` 只在 POSIX
        发事件，Windows 上靠导入那一把锁）。"""
        assert len(set(DENIED_MODULES)) == len(DENIED_MODULES)
        assert len(set(DENIED_EVENTS)) == len(DENIED_EVENTS)
        assert "socket." in DENIED_EVENTS and "subprocess." in DENIED_EVENTS


# ── ⑤ 违规不能被 `except OSError` 吞掉 ─────────────────────────────────────


class TestTheViolationCannotBeSwallowedAsAnOSError:
    """这是全文件**最值钱**的一组，因为写错了会静默回到原状。

    策略代码（尤其 A2 生成的那种）到处是「读不到就算了」的 `except OSError`。
    若 `SandboxViolation` 继承 `OSError`，那些 `except` 会把拦下来的违规当场
    吞掉：策略继续跑、实验继续记分、报告里一处红都没有。理由的全文在
    `sandbox.py` 第五节。
    """

    def test_it_is_not_an_oserror(self) -> None:
        assert not issubclass(SandboxViolation, OSError)
        assert issubclass(SandboxViolation, Exception)

    def test_the_natural_swallowing_pattern_does_not_swallow_it(self) -> None:
        survived = False
        try:
            try:
                POLICY.check_open("config/stage1_config.json")
            except OSError:  # ← A2 最爱写的那一行
                survived = True
        except SandboxViolation:
            survived = False
        assert not survived, (
            "`except OSError` 把沙箱违规吞掉了——"
            "这条路径会静默回到原状（`:401`），必须改回不继承 OSError"
        )

    def test_an_oserror_still_behaves_like_an_oserror(self) -> None:
        """反方向也测一下：真 `OSError` 仍然被 `except OSError` 抓住。

        没有这条，上面那条可以被一个「把所有异常都改名」的改动骗过。
        """
        with pytest.raises(OSError):
            open(REPO_ROOT / "does" / "not" / "exist.json")


# ── ⑥ 钩子真的挂上了吗 ──────────────────────────────────────────────────────


#: 在子进程里装钩子，然后**真的去撞**。这一段是「判定逻辑」与「沙箱生效」之间
#: 唯一的那根线——没有它，本文件其余部分全是空转（见模块 docstring）。
SANDBOX_PROBE = r'''
import json, os, sys

from src.adapter import sandbox

outcome = {}


def attempt(label, fn):
    try:
        fn()
    except sandbox.SandboxViolation as exc:
        outcome[label] = "violation:" + exc.what
    except BaseException as exc:
        outcome[label] = "other:" + type(exc).__name__
    else:
        outcome[label] = "allowed"


def derive_truth():
    from src.adapter.ground_truth import stage1_truth_from_config
    return stage1_truth_from_config("config/stage1_config.json")


policy = sandbox.install()
outcome["idempotent"] = sandbox.install() is policy
outcome["roots"] = [str(r) for r in policy.protected_roots]

attempt("open-relative", lambda: open("config/stage1_config.json").close())
attempt("open-absolute", lambda: open(os.path.join(os.getcwd(), "config", "stage1_config.json")).close())
attempt("open-traversal", lambda: open("config/../config/stage1_config.json").close())
attempt("open-bytes", lambda: open(b"config/stage1_config.json").close())
attempt("listdir", lambda: os.listdir("config"))
attempt("import-socket", lambda: __import__("socket"))
attempt("import-subprocess", lambda: __import__("subprocess"))
attempt("os-system", lambda: os.system("echo hi"))
attempt("derive-truth", derive_truth)
# 反方向：这些**必须**继续能用，否则沙箱会被关掉
attempt("open-legit", lambda: open("pyproject.toml").close())
attempt("open-own-source", lambda: open("src/adapter/surface.py").close())
attempt("import-legit", lambda: __import__("ipaddress"))
attempt("import-strategy", lambda: __import__("src.baselines.b0_probe_all"))
attempt("import-truth-type", lambda: __import__("src.adapter.truth"))

os.write(1, (json.dumps(outcome) + "\n").encode("utf-8"))
'''


class TestInstallHappensOnceAndLoudly:
    """在**真子进程**里装钩子，然后让 `open` / `import` / `os.system` 真的发生。

    为什么非得子进程：钩子摘不掉（见模块 docstring）。这条限制在这里反过来成了
    资产——它保证这一段**不会**意外地影响别的测试。
    """

    def test_the_hook_is_really_on_and_the_scenarios_split_the_right_way(self) -> None:
        report = _child(SANDBOX_PROBE)

        assert report["idempotent"] is True, "同策略重复 install 应该是幂等的"
        assert report["roots"] == [str(REPO_ROOT / "config")]

        for label in (
            "open-relative",
            "open-absolute",
            "open-traversal",
            "open-bytes",
            "listdir",
            "import-socket",
            "import-subprocess",
            "os-system",
            "derive-truth",
        ):
            assert report[label].startswith("violation:"), (
                f"{label} 没被拦下，实际是 {report[label]!r}——"
                "判定逻辑对但钩子没生效，或者这条路绕过了钩子"
            )

        for label in (
            "open-legit",
            "open-own-source",
            "import-legit",
            "import-strategy",
            "import-truth-type",
        ):
            assert report[label] == "allowed", (
                f"{label} 被沙箱误伤成 {report[label]!r}——"
                "沙箱太严会让实验跑不起来，而那时的处置是把它关掉"
            )

    def test_deriving_the_truth_needs_a_read_and_that_read_is_what_dies(self) -> None:
        """**T10 Verify 里那句「断言策略进程无法构造 `GroundTruth`」的操作形式。**

        `stage1_truth_from_config()` 本身不是秘密，是个函数；它**必须读一个
        受保护文件**才能产出真值。于是进程边界那条论证（「够得到类型，够不到
        值」）在这里升级成一个更强的断言：**产出值的那条路被堵死了**。

        比「`hasattr(surface, "ground_truth")` 是假」强，也比「构造出来是个空
        容器」强——前两条只说**此刻**手上没有，这条说**拿不到**。
        """
        assert _child(SANDBOX_PROBE)["derive-truth"] == "violation:读文件"

    def test_a_second_different_policy_is_refused_not_silently_stacked(self) -> None:
        """两道钩子并存时谁生效取决于注册顺序——那是静默失效的形状，
        所以第二次装**不同**的策略必须炸而不是叠加。"""
        code = (
            "import json, os\n"
            "from src.adapter import sandbox\n"
            "sandbox.install()\n"
            "widened = sandbox.SandboxPolicy(protected_roots=())\n"
            "try:\n"
            "    sandbox.install(widened)\n"
            "except RuntimeError as exc:\n"
            "    out = 'refused:' + str(exc)[:20]\n"
            "else:\n"
            "    out = 'STACKED'\n"
            "os.write(1, (json.dumps(out) + chr(10)).encode('utf-8'))\n"
        )
        assert _child(code).startswith("refused:"), (
            "装上第二份策略没有报错——钩子摘不掉，两道并存时谁赢不确定"
        )

    def test_installed_reports_none_before_installation(self) -> None:
        """`installed()` 在没装过的进程里是 `None`——pytest 进程正好是这样。

        **这条同时是一条哨兵**：若哪天有测试在 pytest 进程里装了钩子，这里会
        从 `None` 变成一份策略，而那时整个会话的行为都变了。
        """
        assert sandbox.installed() is None


# ── ⑦ 默认策略 ──────────────────────────────────────────────────────────────


class TestTheDefaultPolicyCoversTheRepoRoot:
    """`default_policy()` 从**模块自己的位置**推仓库根，不靠 `cwd`。

    理由：子进程的 `cwd` 是启动参数给的。一个 `cwd=/` 的子进程若因此失去全部
    保护，那是**静默**的——所以根必须来自代码位置。
    """

    def test_the_repo_root_is_derived_from_the_module_not_the_cwd(self) -> None:
        assert default_policy().protected_roots == (REPO_ROOT / "config",)

    def test_only_config_is_protected(self) -> None:
        """加目录的门槛是「**它里面能算出真值**」。`docs/restricted/` 里是版权
        论文、`.git/` 里是历史——都不是实验的答案，装进去只会制造假红。"""
        assert PROTECTED_SUBDIRS == ("config",)

    def test_an_explicit_root_wins(self, tmp_path: Path) -> None:
        policy = default_policy(tmp_path)
        assert policy.protected_roots == (tmp_path / "config",)
        with pytest.raises(SandboxViolation):
            policy.check_open(tmp_path / "config" / "x.json")

    def test_describe_names_the_roots_and_the_sizes(self) -> None:
        text = default_policy().describe()
        assert str(REPO_ROOT / "config") in text
        assert str(len(DENIED_MODULES)) in text


# ── ⑧ 决策缓存的 runner 侧通道 ──────────────────────────────────────────


class TestTheCacheIsAnOpenItem:
    """缓存查询只能经过 runner 的 `decide`，不能给策略文件能力。"""

    def test_decide_is_the_only_cache_adjacent_verb(self) -> None:
        assert "decide" in protocol.CLIENT_VERBS
        assert not [verb for verb in protocol.CLIENT_VERBS if "cache" in verb]
        assert protocol.PROTOCOL_VERSION == 4

    def test_the_cache_still_has_no_canonical_location(self) -> None:
        for method in (DecisionCache.load, DecisionCache.save):
            parameter = inspect.signature(method).parameters["path"]
            assert parameter.default is inspect.Parameter.empty, (
                f"`DecisionCache.{method.__name__}` 有了默认路径——"
                "那意味着缓存有了规范位置，而它**必须同时**进 "
                "`sandbox.PROTECTED_SUBDIRS`，否则策略进程能直接读它"
            )
        named = [name for name in vars(cache_module) if name.isupper()]
        assert not [name for name in named if "PATH" in name or "DIR" in name], (
            f"`cache` 模块出现了路径常量 {named}——同上，它必须同时进受保护根"
        )

    def test_if_a_cache_root_ever_appears_it_is_here(self) -> None:
        assert PROTECTED_SUBDIRS == ("config",), (
            "受保护根多了一个——若那是决策缓存的位置，正确的修法是把它搬到 "
            "runner 侧经 stdio 提供（`:1482`），而不是在两侧各读一份文件"
        )
