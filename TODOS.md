# TODOS

推迟项清单。来源：`IPv6_Agent_探测_设计方案.md` 的 `## NOT in scope` 表与
`/plan-eng-review`（2026-10-03）的 TODOS 提案，四条均由用户逐条确认为「加进 TODOS.md」。

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
**Depends on:** Stage 1 的 `ProbeSurface` 适配器完成（T2/T10）

---

## 仓库

### 把版权材料与原型代码物理隔离，为将来开源做准备

**What:** 把 IEEE 论文原文与全文译读从原型代码里剥离——拆成两个仓库，或用 `docs/restricted/` 目录 + 导出脚本。

**Why:** 仓库同时装着版权材料（`6Seeks_...pdf`、`6Seeks_中文全文.md`、`IPv6_Agent_Probing_Research_Report.*`）和原型代码 `src/`。`CLAUDE.md` 已把「将来开源必须先剥离」写成硬约束，但**没有任何机制保证这件事会发生**。现在 `src/` 还是空的，拆分成本最低；等代码长起来再拆要处理历史、引用路径与 CI。

**Context:** 仓库**已推送到 GitHub（private）**。两条备选：(a) 拆成 `mentors-task-notes`（版权材料，永久 private）+ `mentors-task-proto`（代码，可开源）；(b) 单仓库 + `docs/restricted/` + 一个导出脚本，导出时排除该目录。**关键是开源可能永远不发生——所以这条可以一直不做，但一旦要做，越早越便宜。**

**Effort:** M
**Priority:** P3
**Depends on:** None

---

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
