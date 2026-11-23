"""决策 prompt —— **它是数据，不是代码**（T5 / 4A 的修法所在）。

## 为什么 prompt 单独成包

bug 1（`:563`）是「键里没有 prompt 版本」：改一句 prompt 后重跑**全量命中旧缓存**，
结果不变，于是被误判为「这次改动无效」。**而 A1 与 A2 的唯一差别就是工具池，
这个 bug 正落在 P1 的对照上。**

修法是「键含 prompt 与工具池的**内容哈希**」（`:563`）。把 prompt 文本放进这个包、
由 `cache.py` 对**渲染后的字节**取摘要，于是「改 prompt ⇒ 键变」不需要任何人记得
做什么——**没有版本号可忘，因为键就来自内容本身**。这是内容哈希比版本号强的地方：
版本号是一种约定，约定会被忘；内容哈希是一种恒等式，忘不掉。

> **哈希的是模型真正看到的那串字节**，不是模板名，也不是模板对象。理由：渲染
> 逻辑本身也是 prompt 的一部分——只改 `render()` 而不改模板，模型看到的东西变了而
> 键没变，正是 bug 1 的形状。所以 `cache.py` 收的是**渲染后**的文本。

## 模板里有什么、没有什么

**有**：任务目标、动作 schema、决策必须给出理由（P1 的表格里"决策理由 | 可读"
那一格）、当前观测状态、可用工具池、剩余预算。

**没有——而且这是刻意的**：任何**识别 aliased 前缀的方法**。题眼是「收到响应常常是
坏消息」（`:138`），而 P1 要测的正是**被测对象能不能自己找到应对办法**。把 FRP 的
签名规则写进 prompt 等于把答案发给考生，A1/A2 那一列就变成了对 B5（手写启发式）
的抄写。B5 的公平性协议（`:1422`）要求 B5 与 A2 **同信息**——两边都不能拿到方法。

## 与 A3 的关系（T16）

知识消融使用 `ablated.py` 中独立的不透明模板。A1/A2 的基准模板现在支持
`reporting=False`：模型只决定预算如何花，最终声明由三行共用的确定性规则完成。
"""

from __future__ import annotations

from collections.abc import Sequence

#: 基准决策模板。
#:
#: 占位符全部由 `render()` 用**已规范化的输入**填：前缀列表与工具池都按名字排序，
#: 状态由 `decide.Stage1State` 提供（它按构造与到达顺序无关）。**渲染必须是纯函数**
#: ——否则键会随渲染过程漂移，而那正是 bug 2 的另一个入口。
DECIDER_TEMPLATE = """\
你是一个 IPv6 地址探测任务的决策体。你只能通过工具与目标网络交互，看不到真值。

## 任务

在给定的一批前缀里探测地址，并就每个前缀回答：**它是不是一个 aliased 前缀**
（该前缀下每一个地址都响应——于是它看起来"很丰富"，实际不含新信息）。
探测预算是有限的，用完之后本轮结束。

## 可用工具

{tools}

## 当前观测

{state}

## 你需要输出的东西

只输出一个 JSON 对象，不要别的东西。允许的形状：

{actions}

规则：
- 除上面列出的键之外**不得有多余的键**（多一个键这一条就会被拒收）。
- `reason` 必填且非空：你的每一个决定都要能被读出来为什么。
- `count` 是本次要探的地址数，不得超过剩余预算。
{rules}
"""


def render(
    *,
    tools: Sequence[tuple[str, str]],
    state_text: str,
    objective_text: str | None = None,
    construction: bool = False,
    reporting: bool = True,
) -> str:
    """把模板渲染成**模型真正看到的那串字节**。

    Args:
        tools: `(name, description)` 对，**调用方负责排序**（`decide.py` 用
            `ToolPool` 的规范化顺序传进来）。这里不再排一次：排序规则若有两处，
            将来改一处就会出现"键与模型看到的不一致"，而那又是一次静默漂移。
        state_text: 状态的规范化文本（`Stage1State.render()`），同样与到达顺序无关。

    Returns:
        渲染后的 prompt 文本。**它的字节就是 `cache.CacheKey` 里 `prompt` 那一个分量。**
    """
    rendered_tools = "\n".join(f"- `{name}`：{description}" for name, description in tools)
    actions = [
        '  {"action": "probe", "prefix": "<前缀>", "count": <正整数>, "reason": "<一句话>"}'
    ]
    if reporting:
        actions.extend(
            [
                '  {"action": "report", "prefix": "<前缀>", "aliased": true|false, "reason": "<一句话>"}',
                '  {"action": "final_report", "reports": {"<前缀>": true|false}, "reason": "<一句话>"}',
                '  {"action": "stop", "reason": "<一句话>"}',
            ]
        )
    if construction:
        actions.append(
            '  {"action": "construct", "name": "<标识符>", "blueprint": "balanced_probe", '
            '"count": <正整数>, "reason": "<一句话>"}'
        )
        actions.append(
            '  {"action": "compose", "name": "<标识符>", "steps": '
            '[{"target": "least_probed|unprobed|responsive|balanced", '
            '"fraction": <0..1>}], "count": <正整数>, "reason": "<一句话>"}'
        )
    rules = (
        "- `aliased` 必须是 JSON 的 true / false，**不是** 1 / 0。\n"
        "- 剩余预算大于 0 时只做探测/构造，`count` 必须恰好等于当前 epoch 的预算。\n"
        "- 花完预算后用一次 `final_report` 提交全部最终判断。"
        if reporting
        else "- 每个决策必须恰好花完当前 epoch 的预算；最终判定由固定报告规则完成。"
    )
    prompt = DECIDER_TEMPLATE.format(
        tools=rendered_tools or "（工具池为空）",
        state=state_text,
        actions="\n".join(actions),
        rules=rules,
    )
    if objective_text:
        prompt += (
            "\n## 在线目标\n\n"
            "以下数值仅由你可见的 Response 推导，不是真值：\n\n"
            f"{objective_text}\n"
        )
    return prompt


__all__ = ["DECIDER_TEMPLATE", "render"]
