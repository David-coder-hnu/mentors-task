"""A2 的执行沙箱 —— **进程边界之外的第二道**（T10 / D7）。

设计方案 `:397` 把强制手段定成**进程边界**，`:401` 紧接着要求**另加沙箱**，
理由是两者的失效方式不同：

> **进程边界防「顺手读到」，沙箱防「刻意去找」。**

`tests/unit/test_isolation.py` 已把前半句做成可红的断言（真子进程 + 导入图 +
出边黑名单）。这个模块做后半句。

---

## 一、为什么进程边界不够：答案躺在磁盘上

`v6 (c)`（`:405`）说得最准：**`config/stage1_config.json` 就是 Stage 1 的
`GroundTruth`**（`:494` 原话），而它是磁盘上的一个文件，就在子进程读得到的
地方。于是「进程边界 + stdio」只挡住了**对象**，没挡住**文件**：

```python
open("config/stage1_config.json")      # ① 拿到全部 *_response_rate
aliased = (rate == 1.0)                # ② 答案，一行
```

这条路**不需要**任何真值对象跨过那条边——它绕开整条通路，所以导入图断言、
出边黑名单、`hasattr` 全都不适用。它就是失败模式表里那一条**唯一还开着的
critical gap**（`:1203`：无测试 + 无错误处理 + 静默）。

## 二、判据不是「有没有测试」，是「失败了会不会安静」

`:405` 给这条断言定的验收标准值得原样抄在这里：

> `config/` 进沙箱**读路径黑名单**，并写一个单测断言「子进程读 `config/` 必须
> 失败」——**这条断言的失败方式是"读成功了"，所以它是可测的**。

所以本模块的全部设计只服务一件事：**把一次成功的静默读取，变成一次当场炸掉
的 `SandboxViolation`。** 判定它有没有做到，看的是「读成功了会不会变红」，不是
「读失败了会不会变绿」。

## 三、审计钩子的能力边界（诚实记录）

强制机制是 `sys.addaudithook`。它在 CPython 里是**进程级、装上就摘不掉**的：
钩子一旦注册，此后每一次 `open` / `import` / `socket.*` 都会先过它，而**从钩子
里抛异常会中止那次调用本身**（实测确认：`open` 抛 `SandboxViolation` 之后文件
根本没被打开）。

它的边界同样要说清，否则这个模块会被读成一个比它更强的东西：

| 挡得住 | 挡不住 |
|---|---|
| `open()` 及其所有上层封装（`pathlib`、`json.load`、`io.open`、`os.open`） | `os.stat`（**CPython 不发这个审计事件**，实测确认）——存在性与大小看得见，内容看不见 |
| `import`（发在模块加载**之前**，所以它是拦得住的那一类） | 已经被 `import` 进 `sys.modules` 的模块（钩子装在 `strategy_child` 的入口，用户代码还没开始跑） |
| `socket.*` / `subprocess.Popen` / `os.system` / `os.exec*` / `ctypes.*` | **原生代码**：一段 C 里的 `open(2)` 不经审计事件 |
| 受保护根的 `os.listdir` / `os.scandir` | 从受保护根之前就打开着的文件描述符 |

「挡不住原生代码」这一条是**为什么 `ctypes` 与 `subprocess` 的封禁是承重的、
而不是锦上添花**：它们是通向「审计事件之外」的两扇门。**剩下的交给更强的层**
（Linux 的 mount namespace：`config/` 干脆不进子进程的文件系统；见第八节）。

## 四、「读路径白名单」在这里为什么落成「受保护根黑名单」

`:401` 的原文写的是**读路径白名单**。在这一层上它落不成白名单，理由是实测的：
**解释器自己也用 `open` 读文件**——导入模块要读 `.py` / `.pyc`，于是审计钩子里
每一次 `import` 后面都跟着几个 `open`（`socket.py`、`__pycache__/socket.cpython-312.pyc`
…）。默认拒绝一切 `open`，第一个 `import` 就死，解释器起不来。

真正的白名单形式是**文件系统视图**（子进程看不到那些文件），那是另一层，见第八
节。在审计这一层上，能成立的规则是**具名的受保护根**：默认只有 `config/`
（唯一有答案的目录），`SandboxPolicy.protected_roots` 是它唯一的扩展点。

> **决策缓存不在默认受保护根里，理由是它今天没有规范路径。** `DecisionCache`
> 的 `load()` / `save()` 收一个显式 `path` 参数（`cache.py`），**没有任何默认值
> 或环境变量**决定它落在哪。所以本模块**指不出**该保护哪个目录。这条约束的
> 落点写在第九节，它仍然是开着的一条。

## 五、为什么 `SandboxViolation` **不**继承 `OSError`

这是本模块最容易写错的一处，而且写错了会**静默回到原状**：

```python
class SandboxViolation(OSError):   # ← 错的
    ...
```

策略代码（尤其是 A2 生成的那种）到处是 `try: open(p) except OSError: pass` ——
「读不到就算了」是极自然的写法。若 `SandboxViolation` 是 `OSError`，那些
`except` 会把拦下来的违规**当场吞掉**，策略继续跑，实验继续记分，而报告里没有
任何一处变红。那正是 `:401` 说的「泄漏是静默的，跑完才发现则全部重跑」。

所以它继承 `Exception`：`except OSError` 抓不到它，`except Exception` 才抓得到
——而写下 `except Exception: pass` 的人至少是在**明确地**吞掉一个写着「沙箱拒绝」
的异常。由 `test_a_violation_is_not_an_oserror` 钉住。

## 六、为什么钩子装在 `strategy_child`，而不是 A2 的专用路径

`:1479` 把沙箱描述成「A2 专用，只有它会执行生成的代码」。**实现上不按那条走**，
理由与 T2(c) 把隔离跟接口同一条提交是同一条：

* **A2 专用 = opt-in = 有一天会忘。** 忘了的表现是「这次实验的沙箱没装」，而它
  **不可观测**——跑出来的数字与装了沙箱时一模一样（只要策略没去读答案）。这
  正是 critical gap 的定义（无测试、无错误处理、静默）。
* **默认装 = 没有忘记这条路径。** 装在所有策略进程上，代价是零：B0–B5 / M1 /
  M2 都不读 `config/`、不开 socket、不起子进程。若哪一个真的读了，那是**它**该
  被拦下的 bug，不是沙箱过严。
* **而且它让断言变便宜**：`tests/unit/test_isolation.py` 里那些真子进程测试走的
  就是生产那条路径（`spawn_strategy` → `strategy_child`），不需要为沙箱再搭一套
  平行的启动方式。**没有第二套启动方式，就没有"两条路不一致"这个 bug 面。**

## 七、禁的是**能力**，不是风格

`denied_modules` 里有 `asyncio` 吗？**没有**，尽管 `tests/unit/test_isolation.py`
的 `test_agent_cannot_reach_the_network` 把 `asyncio` 列进了网络客户端的形状里。
两条规则的目标不同：

* 那条**导入图**断言问的是「这个包有没有**理由**拖进网络栈」，答案是没有，所以
  `asyncio` 出现在它的怀疑名单上是对的。
* 这条**沙箱**规则问的是「这次调用有没有**能力**」——`asyncio` 本身不是能力，
  `socket.__new__` 才是。禁能力不误伤写法：一段用 `asyncio` 组织协程、但
  只走 stdio 的策略代码是合法的，而它一旦想要真正的连接就先撞上 `socket`。

所以名单上只有两类东西：**逃逸口**（`subprocess` / `ctypes` / `pty` / `os.exec*`）
与**网络原语**（`socket` / `ssl` 与几个具名的第三方客户端）。

## 八、更强的层（Stage 2 / Linux，本任务不做）

审计钩子是**可移植的那一层**（Windows 上也能跑，`strategy_child` 是本仓库的
开发环境）。严格更强的做法是把 `config/` 从子进程的文件系统里拿掉：

```bash
unshare -m --map-root-user sh -c 'mount --bind /empty /path/to/repo/config; exec python -m src.adapter.sandbox_child ...'
```

那才是 `:401` 说的「读路径白名单」的正身，而且它连 `os.stat` 一起挡住。**它需要
Linux + `CAP_SYS_ADMIN`，本仓库的开发环境是 Windows**，所以本任务交付的是可移植
那一层，并把这一层记进 `TODOS`，不是假装审计钩子等于命名空间隔离。

## 九、决策缓存的接口约束（T5 递过来，`:1482`；2026-11-10 已接线）

`:1482` 记了一条此前没有落点的接口约束：**决策缓存是能力，不是许可。**

* 数据流图把缓存画在 **runner 进程**里（`:1154`），而第 ③ 条边与 `## 约束` 段都
  写着「策略进程不得读决策缓存文件本身——缓存里存着别的策略的决策」（`:1179`）。
* **本模块给它的落点是：受保护根机制**（把缓存根填进 `protected_roots` 就封住），
  但它**指不出**该填哪个路径——缓存今天由调用方传显式 `path`（第四节）。
* **当前路由是走 stdio**：协议 v4 的 `decide` 动词把 prompt / 规范化状态 / 工具池
  交给 runner 的 `DecisionBroker`，由持有缓存的进程按当前 seed 与完整键回答。
  策略进程只拿回模型原文，拿不到缓存文件内容。
* 缓存路径仍由调用方显式提供；`ExperimentSpec` 会把它作为额外受保护根通过
  `PROTECTED_ROOTS_ENV` 传给子进程，即使 A2 猜中路径，`open()` 仍会失败。

> T10 关掉的是读 `config/`；v4 `decide` 在此之上补了 runner 侧缓存通路。

## 十、让这条路线的边界继续可测

一件开着的事，如果只能靠人记得，它就是下一个 critical gap。所以这里放一条
相关断言（`tests/unit/test_sandbox.py::TestTheCacheIsAnOpenItem`）现在锁定：

* `decide` 是唯一的缓存相关动词，协议版本已升到 v4；
* 若有人给 `DecisionCache` 定了一个默认落盘位置，它**必须**同时进
  `protected_roots`。

显式传入的路径也必须经环境变量加入受保护根；这条是缓存文件层的第二道锁。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DENIED_EVENTS",
    "DENIED_MODULES",
    "PROTECTED_SUBDIRS",
    "SandboxPolicy",
    "SandboxViolation",
    "default_policy",
    "install",
    "installed",
]


class SandboxViolation(Exception):
    """沙箱拒绝了一次能力。**不继承 `OSError`** —— 理由见模块 docstring 第五节。"""

    def __init__(self, what: str, detail: str) -> None:
        super().__init__(f"沙箱拒绝{what}：{detail}")
        self.what = what
        self.detail = detail


# ── 策略 ────────────────────────────────────────────────────────────────────

#: 默认要封死的目录（相对仓库根）。**只有 `config/`** —— 它是唯一装着答案的
#: 那个（`config/stage1_config.json` **就是** Stage 1 的 `GroundTruth`，`:494`）。
#:
#: 加一个目录的门槛是「**它里面能算出真值**」：装进去容易，装错了的代价是把
#: 策略的正常读取变成违规，而那种红是**假红**，会让这条断言的信誉贬值。
PROTECTED_SUBDIRS: tuple[str, ...] = ("config",)

#: 子进程从 runner 显式接收的额外受保护根。路径本身可以可见，读取内容不行；
#: 这样 A2 即使猜到决策缓存位置，`open()` 仍会当场失败。
PROTECTED_ROOTS_ENV = "MENTORS_TASK_PROTECTED_ROOTS"

#: 逃逸口与网络原语。**禁的是能力不是风格**——理由见模块 docstring 第七节。
DENIED_MODULES: frozenset[str] = frozenset(
    {
        # 逃逸口：起新进程 / 调原生代码，两者都能绕开审计事件（第三节）
        "subprocess",
        "multiprocessing",
        "ctypes",
        "_ctypes",
        "cffi",
        "pty",
        "fcntl",
        "_winapi",
        # 网络原语
        "socket",
        "ssl",
        "_ssl",
        "selectors",
        # 网络协议客户端（`socket` 已经堵住它们，列出来是为了错误信息直指问题）
        "http",
        "urllib.request",
        "urllib.error",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "nntplib",
        "xmlrpc",
        "webbrowser",
        # 具名的第三方客户端（`src/` 运行期零第三方依赖，所以它们本来也不该在）
        "requests",
        "httpx",
        "urllib3",
        "aiohttp",
        "openai",
        "anthropic",
    }
)

#: 直接封的事件前缀（`socket.__new__` / `subprocess.Popen` / `os.system` …）。
#: 与 `DENIED_MODULES` 是**两道独立的锁**：导入名单管「拿不拿得到工具」，
#: 事件名单管「这把工具还能不能使」——例如模块已在 `sys.modules` 里的时候。
DENIED_EVENTS: tuple[str, ...] = (
    "socket.",
    "subprocess.",
    "ctypes.",
    "_winapi.CreateProcess",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.startfile",
    "os.fork",
    "pty.spawn",
)


def _resolve(path: object) -> Path | None:
    """把审计事件里那个路径解析成绝对规范形式。**拿不准就返回 `None`。**

    `realpath` 而不是 `normpath`：前者会跟到符号链接的**终点**，于是
    「`./link_to_config/x.json`」这类绕法也落在受保护根里面。它不做任何
    `open`，所以不会在钩子里递归。

    **路径的形状有三种，`str` 只是其中之一。** `os.fspath` 一次收齐：
    `str` / `bytes` / 任何 `os.PathLike`（`pathlib.Path` 是第三种）。这不是
    洁癖——`_resolve` 返回 `None` 的语义是**「没有可判的路径，放行」**，所以
    「不认识这种形状」与「没有路径」必须是两件事。判错方向的后果是**失败即放行**：
    审计事件确实总给 `str`（`_io.open` 先过一遍 `os.fspath` 才发事件），但
    `root_of` 是公开方法，测试与将来的调用方都会传 `Path`，而那时它会**静默
    不设防**。写这个模块时正是这样漏过一次，被 `test_the_absolute_form_is_refused_too`
    抓住。

    非路径的东西（`open(fd)` 那种整数）返回 `None`——那确实没有路径可判。
    """
    try:
        raw = os.fspath(path)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    try:
        return Path(os.path.realpath(raw))
    except (OSError, ValueError):  # pragma: no cover - 平台/畸形路径
        return None


def _is_same_or_below(target: Path, root: Path) -> bool:
    """判断 `target` 是否等于 `root` 或其子路径。

    纯词法比较在大小写不敏感的文件系统上不够：macOS 默认 APFS 会让
    `CONFIG/stage1_config.json` 真的打开 `config/stage1_config.json`，但
    `POSIXPath` 仍按大小写区分字符串。只看 `is_relative_to()` 会把这个
    实际存在的别名放过去。

    因此先做不碰文件系统的词法判断，再用 `samefile` 比较现存祖先。后一步
    只在需要时才调用，且不读取文件内容，所以不会在审计钩子里递归。对于
    `CONFIG/does-not-exist.json` 这类目标，只要 `CONFIG` 目录本身与 `config/`
    是同一 inode，仍然能判中。
    """
    if target == root or target.is_relative_to(root):
        return True

    for candidate in (target, *target.parents):
        try:
            if candidate.exists() and os.path.samefile(candidate, root):
                return True
        except OSError:
            # 权限不足、路径在检查期间消失等情况下继续看更高层祖先；真正的
            # 文件系统错误会由随后被拒绝或失败的打开操作自行暴露。
            continue
    return False


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱策略 —— **它是数据，不是行为**。

    这是本模块最承重的一个结构决定：强制机制（`sys.addaudithook`）装上就摘不掉、
    而且只能装进**一个**进程，所以它没法在 pytest 进程里被测。把策略抽成纯函数
    之后，全部判定逻辑都能用普通单测穷举（`tests/unit/test_sandbox.py`，不起任何
    子进程），而真子进程测试只剩**一条**：确认那几个纯函数真的挂在了钩子上
    （`tests/unit/test_isolation.py::TestTheSandboxIsOnTheRealChild`）。
    """

    #: 封死的根，**已解析成绝对路径**。判定用「是不是在它下面」，不是字符串前缀。
    protected_roots: tuple[Path, ...] = ()
    denied_modules: frozenset[str] = DENIED_MODULES
    denied_events: tuple[str, ...] = DENIED_EVENTS

    # ── 判定（全是纯函数，可穷举单测）──────────────────────────────────

    def root_of(self, path: object) -> Path | None:
        """`path` 落在哪个受保护根里？不在任何一个里就返回 `None`。"""
        target = _resolve(path)
        if target is None:
            return None
        for root in self.protected_roots:
            # `is_relative_to` 比字符串前缀严：`/repo/configs` 不在 `/repo/config` 里。
            # `_is_same_or_below` 再补上 macOS 默认大小写不敏感文件系统的身份判断。
            if _is_same_or_below(target, root):
                return root
        return None

    def check_open(self, path: object) -> None:
        """`open()` —— 读、写、`pathlib`、`json.load` 全部走这一个事件。"""
        root = self.root_of(path)
        if root is not None:
            raise SandboxViolation(
                "读文件",
                f"{path!r} 落在受保护根 {root} 里——那里装着真值"
                "（config/*.json 就是 GroundTruth 的来源，设计方案 `:494`）。"
                "策略侧拿到的任务范围只有握手给的那串前缀（`:395` 的 (c) 条）。",
            )

    def check_listdir(self, path: object) -> None:
        """列目录。**根是「封死」的，不是「只读封死」**——名字也算泄漏面。"""
        root = self.root_of(path)
        if root is not None:
            raise SandboxViolation("列目录", f"{path!r} 落在受保护根 {root} 里")

    def check_import(self, module: object) -> None:
        """`import`。判定按**点分边界**，不是字符串前缀。

        `socket` 在名单上，`socketserver` 不在（它是另一个模块名）——虽然它
        `import socket`，于是照样被拦，而且是拦在**它**那一层，错误信息指向
        真正的原因。写成裸前缀匹配的话，`socket` 会把一批无关模块一起误伤，
        而误伤的表现是「策略莫名其妙起不来」。
        """
        if not isinstance(module, str):
            return
        for denied in self.denied_modules:
            if module == denied or module.startswith(denied + "."):
                raise SandboxViolation(
                    "导入模块",
                    f"{module!r} 命中 {denied!r}——策略进程只有 stdio 一条出边"
                    "（`:397`），发包由 runner 持 socket 完成，策略不自己开。",
                )

    def check_event(self, event: str, args: tuple[Any, ...]) -> None:
        """审计钩子的总入口：先按事件名分流，再按前缀封。"""
        if event == "open":
            self.check_open(args[0] if args else None)
            return
        if event in {"os.listdir", "os.scandir"}:
            self.check_listdir(args[0] if args else None)
            return
        if event == "import":
            self.check_import(args[0] if args else None)
            return
        for denied in self.denied_events:
            if event == denied or event.startswith(denied):
                raise SandboxViolation("执行", f"审计事件 {event!r} 命中 {denied!r}")

    def describe(self) -> str:
        """一行摘要，进错误信息与日志。"""
        roots = ", ".join(str(r) for r in self.protected_roots) or "（无）"
        return (
            f"受保护根: {roots}；"
            f"禁导入 {len(self.denied_modules)} 项；"
            f"禁事件 {len(self.denied_events)} 项"
        )


def default_policy(
    repo_root: str | os.PathLike[str] | None = None,
    *,
    extra_roots: tuple[str | os.PathLike[str], ...] = (),
) -> SandboxPolicy:
    """默认策略：封 `PROTECTED_SUBDIRS`，用 `DENIED_MODULES` / `DENIED_EVENTS`。

    `repo_root` 默认由**本文件的所在位置**推出来（`src/adapter/sandbox.py` 往上
    三级），不是靠 `cwd`：子进程的 `cwd` 是启动参数给的，而受保护根不能跟着它走
    ——一个 `cwd=/` 的子进程会因此失去全部保护，而那是静默的。
    """
    root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    roots = [(root / sub).resolve() for sub in PROTECTED_SUBDIRS]
    roots.extend(Path(extra).resolve() for extra in extra_roots)
    return SandboxPolicy(protected_roots=tuple(dict.fromkeys(roots)))


# ── 装钩子 ──────────────────────────────────────────────────────────────────

_INSTALLED: SandboxPolicy | None = None


def installed() -> SandboxPolicy | None:
    """本进程装着的那份策略；没装过就是 `None`。"""
    return _INSTALLED


def install(policy: SandboxPolicy | None = None) -> SandboxPolicy:
    """给本进程装上沙箱。**装上就摘不掉**（`sys.addaudithook` 的性质）。

    Raises:
        RuntimeError: 已经装过**另一份**策略。这时候必须炸，不能装第二道：
            两道钩子都在跑，而调用方以为只有一个生效——**谁赢取决于注册顺序**，
            这种歧义正好是「静默失效」的温床。`sys.addaudithook` 没有对应的
            `remove`，所以唯一诚实的处置是拒绝。（同策略重复调用是幂等的。）
    """
    global _INSTALLED
    resolved = policy if policy is not None else default_policy()

    if _INSTALLED is not None:
        if _INSTALLED != resolved:
            raise RuntimeError(
                "本进程已装着一份不同的沙箱策略，而审计钩子摘不掉——"
                "两道钩子并存时谁生效取决于注册顺序，那是静默失效的形状。"
                f"已装：{_INSTALLED.describe()}；要装的：{resolved.describe()}"
            )
        return _INSTALLED

    def _hook(event: str, args: tuple[Any, ...]) -> None:
        # `_INSTALLED` 在这里才是权威 —— 装进闭包的会是**装的那一刻**那份策略，
        # 而 `install()` 之后的任何替换都该由上面那个 RuntimeError 拦住。
        active = _INSTALLED
        if active is not None:
            active.check_event(event, args)

    # 先赋值再注册：`addaudithook` 本身会发一个 `sys.addaudithook` 事件，
    # 钩子在那之前就得能读到策略。
    _INSTALLED = resolved
    sys.addaudithook(_hook)
    return resolved
