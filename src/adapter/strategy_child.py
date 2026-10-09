"""策略子进程的入口 —— `python -m src.adapter.strategy_child --entry mod:run`

**这个模块的导入图定义了策略进程能看到什么。** 它导入 `surface`、`protocol` 与
`sandbox`，```不导入 `oracle` / `truth` / `runner` / `probe_pool` / `iface` ``。
`tests/unit/test_isolation.py::TestImportGraph` 在一个干净子进程里断言这件事。

## 沙箱装在这里，而且是**无条件**的（T10）

`main()` 在 `load_entry` **之前**调 `sandbox.install()`，于是**每一个**策略进程
都带着沙箱跑，不区分 A2 与其他策略。理由在 `sandbox.py` 的模块 docstring 第六节：
「A2 专用」等于 opt-in 等于有一天会忘，而忘了的表现（这次实验没装沙箱）**不可
观测**——跑出来的数字与装了一样。装在唯一那条启动路径上则没有第二套走法。

位置是关键：**在 `load_entry` 之前**，所以用户代码 import 进来的每一个模块都要
过 `sandbox` 的检查；**在 `_hello` 之前**，所以连握手都跑在沙箱里。而它必须在
本模块自己的 import **之后**——那些是我们的代码，且钩子摘不掉，早装只会让
`strategy_child` 自己去撞自己的名单。

## 为什么 stdout 被换成了 stderr

**stdout 就是协议通道本身。** 策略代码里一句手滑的 `print()` 会把一行 JSON
劈成两半，症状是「协议解析偶发失败」——一个指向错误的报错，因为出错的
地方（策略打印）与炸掉的地方（协议解析）隔了半个进程。

所以在握手之前先把真正的 stdout 存进 `_wire_out`，然后把 `sys.stdout` 指向
`sys.stderr`。策略里 `print()` 照常工作，只是去了 stderr。**这不是洁癖**：
它在结构上让「污染协议」这件事没有代码路径，而不是靠策略作者记得别打印。

## 握手是必需的，不是仪式

前缀列表由 runner 在**运行时**给（`v6 (c)`：`ProbeSurface` 的构造参数只有
去重后的前缀列表，`*_response_rate` 留在 runner 侧）。所以子进程启动时
并不知道任务范围——它必须先 `hello` 问一次。
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from collections.abc import Callable

from . import protocol, sandbox
from .protocol import LineChannel, Reply, Request, reply_from_wire, request_to_wire
from .surface import ProbeSurface, SurfaceError

#: 策略入口的函数名。契约是 `run(surface) -> None`。
DEFAULT_ENTRY_FUNCTION = "run"

#: 握手用的请求 id。真实请求从它之后开始编号。
_HELLO_ID = 0


def _hello(channel: LineChannel, timeout_hint: str = "") -> Reply:
    """握手：拿任务范围与初始预算视图。

    **返回整条 `Reply`，不只 `result`。** 初始预算挂在 `Reply.budget_left` 上，
    而不是 `result` 里——只要 `result`，那份预算是被静默丢掉的，于是策略在
    第一次探测之前读 `surface.budget_left` 会拿到 `None`（见 `ProbeSurface`
    `__init__` 里那条接口发现）。这是 T13 写 B0 时撞到的真缺口，不是洁癖。
    """
    channel.send(
        protocol.request_to_wire(Request(verb="hello", args={}, id=_HELLO_ID))
    )
    raw = channel.recv()
    if raw is None:
        raise SystemExit("runner 在握手阶段就关了通道")
    reply = reply_from_wire(raw)
    if not reply.ok:
        raise SystemExit(f"握手被拒: {reply.error}")
    if not isinstance(reply.result, dict):
        raise SystemExit("握手的回复不是对象")
    if reply.result.get("protocol") != protocol.PROTOCOL_VERSION:
        # **版本不匹配必须显式拒绝**：跨进程协议静默降级的表现是数据错位
        # （字段还在、语义变了），比直接崩难查得多。见 protocol.PROTOCOL_VERSION。
        raise SystemExit(
            f"协议版本不匹配：runner 说 {reply.result.get('protocol')}，"
            f"本进程是 {protocol.PROTOCOL_VERSION}"
        )
    return reply


def load_entry(entry: str) -> Callable[[ProbeSurface], None]:
    """把 `"pkg.module:func"` 解析成一个可调用对象。

    `entry` 里带 `:` 时用它后面的名字，否则用 `run`。**在构造 surface 之前
    调用**：入口不存在时要在花掉一次握手之前就炸。
    """
    module_name, _, func_name = entry.partition(":")
    func_name = func_name or DEFAULT_ENTRY_FUNCTION
    module = importlib.import_module(module_name)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise SystemExit(f"{module_name} 里没有可调用的 {func_name}")
    return func


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="strategy_child")
    parser.add_argument("--entry", required=True, help="策略入口，`pkg.module:func`")
    args = parser.parse_args(argv)

    # 先把真正的 stdout 存下来，再把 sys.stdout 指向 stderr。顺序不能反。
    wire_out = sys.stdout
    sys.stdout = sys.stderr
    channel = protocol.JsonLineChannel(sys.stdin, wire_out)

    # **沙箱先于一切用户代码。** 装在 `load_entry` 之前，所以策略 import 进来的
    # 每一个模块都过检查；装在 `_hello` 之前，所以握手也跑在里面。理由与它的
    # 位置见模块 docstring，「A2 专用」为什么不取也在那里。
    sandbox.install()

    try:
        run = load_entry(args.entry)
        bootstrap = _hello(channel)
        prefixes = bootstrap.result
        assert isinstance(prefixes, dict)  # _hello 已经查过
        surface = ProbeSurface(
            list(prefixes["prefixes"]),  # type: ignore[arg-type]
            channel,
            # 把握手带回来的初始预算视图接上——不接的话它在第一次探测前是
            # `None`，而策略要在发第一个包之前就决定怎么花。见 `_hello`。
            budget_left=bootstrap.budget_left,
        )
        run(surface)
        return 0
    except SurfaceError as exc:
        print(f"策略被 runner 拒绝: {exc}", file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except BaseException:
        # 把策略的 traceback 原样留在 stderr。子进程崩溃若只留下一个非零
        # 退出码，runner 侧会把它读成「策略跑完了」，两边都不报错。
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 由 subprocess 走到
    raise SystemExit(main())
