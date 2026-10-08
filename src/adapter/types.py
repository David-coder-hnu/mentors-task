"""靶场适配层的**公开**契约类型 —— 设计方案 `## 接口契约` 段的代码化。

这个模块是**唯一的** `Response` 定义处。策略侧进程与 runner 侧**都**导入它，
所以它**不得**含有任何真值类型或真值字段（见 `## 能力隔离` 段：交给策略的
对象只有 `ProbeSurface`，且构造参数只有去重后的前缀列表）。

> **v7（2026-10-08，T2(c)）：真值类型已搬到 `truth.py`。** 上面那句「不得含有
> 真值」原先与代码直接冲突——`Stage1Truth` 就定义在本模块里，而策略进程必须
> 导入本模块。理由与边界见 `truth.py` 的 docstring。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── ICMPv6 常量（IANA）────────────────────────────────────────────────
ICMP6_DEST_UNREACH = 1
ICMP6_ECHO_REQUEST = 128
ICMP6_ECHO_REPLY = 129

ResponseStatus = Literal["received", "sent_no_reply", "send_failed"]


@dataclass(frozen=True, slots=True)
class Response:
    """一次探测的结果。

    `status` 是**三态**，不是两态 —— 这是 v6 重写这组类型的全部理由
    （设计方案 `:338`）：原文用 `src=None` 表示「无响应」，又另设
    `timeout: bool`，同一件事编码两次，而**「发包失败」无处安放**。
    塌成两态就会让丢包对账把「网卡没起来」记成「地址不存在」。
    """

    status: ResponseStatus
    dst: str
    #: 真实来源地址。仅 `received` 时非 None。
    #: Stage 1 下 Punching Bag 会伪造 src = 我们探的地址，所以通常 src == dst。
    src: str | None = None
    #: 仅 `send_failed`：无 CAP_NET_RAW / 无路由 / 接口不可用。
    error: str | None = None
    icmp_type: int | None = None
    icmp_code: int | None = None
    #: 回包 hop limit，用于推断距离。
    hop_limit: int | None = None
    rtt_ms: float | None = None

    def __post_init__(self) -> None:
        # 三态的字段约束是**可断言**的，所以在这里钉死，
        # 而不是靠消费方记得分支。
        if self.status == "received":
            if self.src is None:
                raise ValueError("received 必须有 src")
            if self.error is not None:
                raise ValueError("received 不得带 error")
        elif self.status == "send_failed":
            if self.error is None:
                raise ValueError("send_failed 必须带 error")
            if self.src is not None:
                raise ValueError("send_failed 不得有 src")
        elif self.status == "sent_no_reply":
            if self.src is not None or self.error is not None:
                raise ValueError("sent_no_reply 既无 src 也无 error")

    @property
    def indirect(self) -> bool:
        """回包来自一个**未被寻址**的实体。

        定义为 `dst != src`，且只在 `received` 时可能为真。Stage 1 下恒为
        False（Punching Bag 伪造 src = dst）；它是给 Stage 2 的 `RouterShim`
        用的（T15）—— 那里合成的最后一跳会让 src 与 dst 分离。

        做成**派生属性而非字段**：一个可独立赋值的 `indirect` 字段与
        `src`/`dst` 之间没有一致性约束，那正是 D7 要防的那类静默不一致。
        """
        return self.status == "received" and self.src is not None and self.src != self.dst
