# TODOS

推迟项清单。来源：`IPv6_Agent_探测_设计方案.md` 的 `## NOT in scope` 表、
`/plan-eng-review`（2026-10-03）与 `/plan-ceo-review`（2026-10-03）的 TODOS 提案，
各条均由用户逐条确认为「加进 TODOS.md」。

优先级定义见 gstack `review/TODOS-format.md`：P0 阻塞 / P1 关键 / P2 重要 / P3 可选 / P4 某天。

---

## 文档

### 录入导师答复与 6Seeks 动作空间清单

**What:** 把「数据可得性」那一问的导师答复（结论 + 原话）写进设计方案的 Open Question 1；把已产出的 6Seeks 动作空间清单贴成 `6Seeks_动作空间清单.md`。

**Why:** 用户已于 2026-10-03 确认「这两个我都做过了」，但**两项结论都没落进仓库**——这是当前最大的记录缺口。Open Question 1 曾是 v1 认定的「唯一能杀死项目的风险」，而它的唯一出处就是那次对话。

**Context:** 设计方案 `## The Assignment` 目前只记了「已完成」这个状态，没记内容。v4 之后该问题的优先级下降（D12 使 `ReplayNet` 不再是真值来源，只剩 Stage 3 的规模需要它），但它仍是 Stage 3 的门。下次会话第一件事就做这个——问一句就有，不问就永远悬着。

**Effort:** S
**Priority:** P1
**Depends on:** None

---

## 实验

### 复现其余 5 个 TGA 基线（6Hit / 6Scan / 6Tree / AddrMiner-S / DET）

**What:** 把 Punching Bag 论文表 3 评测过的六个 TGA 中剩下的五个也接进八行表，而不只是引用它们的已发表数字。

**Why:** 现在表里的外部对手只有 6Sense 一个，且它已被 D7 标注为**预言机辅助上界**（它拿到了 aliased 前缀列表，那正是实验要推断的东西）。多跑几个已发表 TGA 能把对比带从「一个上界」变成「六个同行评审过的算法」。

**Context:** Punching Bag 论文已经在**同一个靶场**上跑过这六个，并报告了各自的毛病——预算不遵守（6Tree 超、6Hit 欠）、6Scan 报数最多超 39% 且 peak-rate 超限、除 6Sense 外全部在 aliasing 上翻车（表 2：其余五个 98.69%–99.98% 的探测打在 aliased 前缀上）、**6Tree 的 APD 会崩**。所以这条的真实决策是「**复现** 还是 **引用**」——论文已发表过数字，同表混用会招来「条件是否可比」的质疑。**已知坑：6Tree 会崩，需单独处理。**

**Effort:** L
**Priority:** P3
**Depends on:** Stage 2 的依赖，**不是 Stage 1 的**（2026-10-03 `/plan-eng-review` v6 更正）。原行写「Stage 1 的 `ProbeSurface` 适配器完成（T2/T10）」，**方向对但落点错**：本条要接的是**八行表**，而八行表归 Stage 2；`ProbeSurface` 只是这条路上的一个中间件，做完它这些 TGA 一个都跑不起来。真实前置是三条——**`T6b`**（B2/B3 落地，给对比带补上同构的实现基线）、**`T15`**（`RouterShim`，没有它 Stage 2 的真值是空的）、以及**6Sense 的独立轨道**（`T7`，它是这条「同行评审算法对比带」现有的唯一成员）。**换句话说：这条任务的触发时点是「Stage 2 的表已经在跑」，不是「接口写完了」。**

---

## 仓库

### fork 路线的回退预案

**What:** 写下 Stage 2 从 `RouterShim` 回退到「fork Punching Bag 包路径」的触发条件、代价与前置要求。

**Why:** D9 把 Stage 2 改成 Response 层 shim，理由是 P4 本就承认 Stage 2 无外部效度、包级真实感买不到东西。**但若这个前提被推翻（导师说「必须真包」），理由就不成立了。** 而回退路径是一个 C++17 raw-socket/libnet/libpcap 的系统任务（合成合法 ICMPv6 错误：正确校验和、合成源地址、hop-limit 语义），**从没有任何工作量估算覆盖过它**，且会直接落在关键路径上。

**Context:**
- **触发条件**：导师或审稿人明确要求线上行为级仿真。
- **代价**：C++ 系统工作量（未估算）+ 必须补做逐包等价性测试（Concern #6：同一 config、同一 seed 下 fork 与原版必须逐包等价，进 CI）—— 这个测试必须在写第一行 fork 代码**之前**写好。
- **注意**：本项目作者对 IPv6 不熟悉，且这是全项目风险最高的一段。

**Effort:** S（仅写预案；执行是 XL）
**Priority:** P4
**Depends on:** None

---

## 工具链

来源：`/plan-ceo-review`（2026-10-03）的 Outside Voice 与 JSONL 产物两步。

### 安装 `jq`

**What:** 在开发机装上 `jq`（Windows：`winget install jqlang.jq` 或 `scoop install jq`）。

**Why:** gstack 的评审技能用 `jq -nc` 逐行序列化任务清单（JSONL），供 `/autoplan` 跨轮次聚合。**规格明令不得手写 JSONL**——手拼 JSON 遇到引号、换行、反斜杠就会产出坏文件，而坏文件在聚合端看起来像合法数据。本机没有 `jq`，所以 `/plan-ceo-review`（2026-10-03）的 21 条 Implementation Tasks **没有产出 JSONL**，那次评审不进 autoplan 聚合。

**Context:** 装好后可重跑那次评审的 JSONL 产出步骤补上。**不影响任何实验结果**，只是少一份跨轮次数据。同类工具缺口见下一条。

**Effort:** S
**Priority:** P3
**Depends on:** None

---

### 安装 Codex CLI，让评审有跨模型覆盖

**What:** `npm install -g @openai/codex`，使 `/plan-ceo-review` 与 `/plan-eng-review` 的 Outside Voice 步骤走真正的跨模型评审，而不是回退到 Claude 子代理。

**Why:** Outside Voice 的设计意图是**用另一个厂商的模型独立审一遍**——不同模型的盲区不同，这是抓出「自己看不见的错误」最有效的手段。`/plan-ceo-review`（2026-10-03）返回 `CODEX_MODE: not_installed`，因此回退到 Claude 子代理：**16 条发现与我自己的视角同源**。事后逐条核实发现其中 **3 条断言站不住**（最典型的是「B5 按构造恰好正确」——它默认了四档响应率的数值，而那恰是未决的 0.2(b) 问题）。**同源评审更容易放过这一类。**

**Context:** 本次回退是规范允许的，不是错误；记下来是为了避免下次误以为「已经跨模型验证过了」。**装它的最佳时机是第一次冒烟表读数之后**——那时做评审才又有实测数据可依（见下）。**注意与「停止评审」条件的关系**：`/plan-ceo-review` 设的条件是「门 0 通过之后、首次冒烟表之前不再开新评审轮」，所以在那之前装它是没有用的。

**Effort:** S
**Priority:** P3
**Depends on:** None

---

## Completed

### 把版权材料与原型代码物理隔离，为将来开源做准备

**Completed:** /plan-ceo-review 2026-10-03（未发版，记 commit `b95aac3`）

**结论：从 TODOS 升级为 Stage 0 的交付物**，不再推迟。原条目内容（拆仓库或 `docs/restricted/` + 导出脚本）原样保留为实现方向，见 `IPv6_Agent_探测_设计方案.md` 与 CEO plan 的 Section 10A。

**为什么提前做：** `CLAUDE.md` 已把「将来开源必须先剥离」写成硬约束，而**没有任何机制保证它会发生**。本条自己记着「**现在 `src/` 还是空的，拆分成本最低**」——一条自己论证「越早越便宜」的推迟项，与「推迟」这个动作本身矛盾。它被推迟的理由（「开源可能永远不发生」）只否定了**紧迫性**，没有否定**成本曲线**。

**落地形式：** 不是文档，而是**可运行的检查**——`docs/restricted/` 收版权材料 + 一个导出脚本按排除清单产出 `src/` 的干净副本，脚本内**断言副本不含任何版权材料路径**。这条断言是门 0 的条件 9（9 条条件之一），**不过就不开门 1**。
