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

### 完成 A1/A2/A3 的 Stage 2 正式表

**What:** 跑完 A1/A2/A3 的剩余三个预注册场景，产出可回放决策缓存，并完成 A1/A2/A3
同 seed 配对与 B5-open 对等比较；在此之前不得把 T16 标为完成。

**Why:** 协议 v4 已把模型调用与缓存移到 runner 侧；A3 轨道、三行共用确定性报告、
运行期 `compose` 程序、B5-open、四场景 30-seed fixture、功效敏感度与构造审计包
都已通过，原先 A2 最终声明的 provider 超时已从设计上消除。剩余问题不是执行链路：
provider 凭据已轮换，`broad` 的 30-seed 真实矩阵和 B5 同场景对照已完成；
`prefix_diversity`、`sparse_trap`、`hot_hierarchy` 仍需模型调用。A3 本轮有
step-only 输出兼容偏差，确认性知识消融应在固定输出 schema 后复跑。B5-open
已在独立留出拓扑上冻结程序，但构造物仍需非场景作者裁决，真实网络稳健性也尚未验证。

**Context:** 证据见 `docs/results/t16-engineering-2026-11-10.md`。模型调用入口是
`scripts/responses_completion.py`；缓存与 agent 结果都在 `docker/run/`，不进
版本库。现有真实运行没有保留 token usage，后续正式表需要费用账本。

**Effort:** M
**Priority:** P1
**Depends on:** 可用的 Stage 2 completion provider

---

### 复现其余 5 个 TGA 基线（6Hit / 6Scan / 6Tree / AddrMiner-S / DET）

**What:** 把 Punching Bag 论文表 3 评测过的六个 TGA 中剩下的五个也接进八行表，而不只是引用它们的已发表数字。

**Why:** 现在表里的外部对手只有 6Sense 一个，且它已被 D7 标注为**预言机辅助上界**（它拿到了 aliased 前缀列表，那正是实验要推断的东西）。多跑几个已发表 TGA 能把对比带从「一个上界」变成「六个同行评审过的算法」。

**Context:** Punching Bag 论文已经在**同一个靶场**上跑过这六个，并报告了各自的毛病——预算不遵守（6Tree 超、6Hit 欠）、6Scan 报数最多超 39% 且 peak-rate 超限、除 6Sense 外全部在 aliasing 上翻车（表 2：其余五个 98.69%–99.98% 的探测打在 aliased 前缀上）、**6Tree 的 APD 会崩**。所以这条的真实决策是「**复现** 还是 **引用**」——论文已发表过数字，同表混用会招来「条件是否可比」的质疑。**已知坑：6Tree 会崩，需单独处理。**

**Effort:** L
**Priority:** P3
**Depends on:** Stage 2 的依赖，**不是 Stage 1 的**（2026-10-03 `/plan-eng-review` v6 更正）。原行写「Stage 1 的 `ProbeSurface` 适配器完成（T2/T10）」，**方向对但落点错**：本条要接的是**八行表**，而八行表归 Stage 2；`ProbeSurface` 只是这条路上的一个中间件，做完它这些 TGA 一个都跑不起来。真实前置是三条——**`T6b`**（B2/B3 落地，给对比带补上同构的实现基线）、**`T15`**（`RouterShim`，没有它 Stage 2 的真值是空的）、以及**6Sense 的独立轨道**（`T7`，它是这条「同行评审算法对比带」现有的唯一成员）。**换句话说：这条任务的触发时点是「Stage 2 的表已经在跑」，不是「接口写完了」。**

---

### ~~把标定出的池大小上界变成一条会红的检查，而不是文档里的一个数~~（已完成）

**What:** 让「池大小 ≤ 无丢包上界」由一个**可执行的断言**保证，而不是靠人记得别把 `max_in_flight` 设太大。

**Why:** T2(b) 标定出本环境无丢包的最大在途数是 **512**（见 [`docs/perf_baseline.md`](docs/perf_baseline.md)），而**目前没有任何东西阻止有人把它设成 4096**。设大了不会报错——靶场的三层对账在所有档位都报「干净」，我们这边的丢包表现为「这个地址没回包」，而**那正好是实验要测量的那个量**。所以这不是性能问题，是**把噪声写成了信号**。设计方案的失败模式表已把它单列为一行（第八条静默失效路径）。

**Context:**
- **为什么难做**：上界是**环境属性**（`SO_RCVBUF` 425984 字节 ÷ skb `truesize`），不是代码常量——同一个池大小在另一台机器上可能是安全的。所以断言不能写死 512。
- **可行方向**：标定产出一份**机器可读**的上界（如 `config/perf_ceiling.json`，含环境指纹），T13 写 config 时读它；启动时若 `max_in_flight` 超过该环境的上界则**拒绝启动**（而不是警告——警告会被人忽略）。
- **另一条更直接的路**：把 `perf_sweep` 的第 ①~④ 步接进 CI 的一个手动 job，每次换环境跑一次，结果直接进 `docs/perf_baseline.md`。
- **触发时点**：T13（写 `config/stage1_config.json`）落池大小的时候。**在那之前池大小根本还没有取值**，所以现在做会过早。

**Effort:** S
**Priority:** P2
**Depends on:** T13（config 里定下池大小的地方）

**Completed:** 2026-11-09。`probe_executor()` 在创建原始套接字之前拒绝
`max_in_flight > CALIBRATED_POOL_CEILING`，由 `tests/unit/test_real_executor.py`
钉住。

---

### 用 Linux mount namespace 把 `config/` 从策略进程的文件系统里拿掉

**What:** 让策略子进程**看不见** `config/`，而不只是「读到会被拦」。

**Why:** T10 交付的沙箱（`src/adapter/sandbox.py`，2026-10-09）是**审计钩子**那一层：它阻断 `open` / `listdir` / `scandir` / `import` / `socket.*` / `subprocess.*`，但**挡不住 `os.stat`**（CPython 不发这个审计事件，实测确认）——存在性与大小看得见；也**挡不住原生代码**（一段 C 里的 `open(2)` 不经审计事件）。严格更强的做法是让那些文件**根本不在子进程的文件系统里**，那才是设计方案 `:401` 说的「读路径白名单」的正身，而且它连 `os.stat` 一起挡住。

**Context:**
- **做法**：`unshare -m --map-root-user sh -c 'mount --bind /empty <repo>/config; exec python -m src.adapter.strategy_child …'`。用一个空目录盖住 `config/`。
- **为什么现在不做**：需要 Linux + `CAP_SYS_ADMIN`，而**本仓库的开发环境是 Windows**。T10 交付的是可移植那一层，并把这一层记在这里——**不是假装审计钩子等于命名空间隔离**（`sandbox.py` 模块 docstring 第八节）。
- **两层不是替代关系，是纵深**：审计钩子给出的是**当场炸的 `SandboxViolation`**（可诊断、带路径）；命名空间给的是 `ENOENT`——**看起来像「文件不存在」，是静默的另一种形式**。而 `:405` 那条判据要的正是「失败方式是读成功了，所以可测」，所以两层都要留。
- **代价要连带处理**：多一层启动包装之后，「生产那条启动路径」就变成两条，会削弱 T10 里「**没有第二套启动方式，就没有两条路不一致这个 bug 面**」那个优点。做的时候要么让策略子进程**只有**命名空间这一条路（探测/开发机各自选层，而不是同时留两条），要么补一条两层的等价性测试。
- **触发时点**：Stage 2 在 Linux 上跑（T15/T16 那条线）时。**在那之前它没有可跑的地方**，现在做会过早。

**Effort:** S（写包装脚本）/ M（接进启动路径 + 两层的等价性测试）
**Priority:** P3
**Depends on:** Stage 2 的 Linux 运行环境（T15/T16）；`src/adapter/sandbox.py`（已交付）

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

### ~~安装 `jq`~~（已完成）

**What:** 在开发机装上 `jq`（Windows：`winget install jqlang.jq` 或 `scoop install jq`）。

**Why:** gstack 的评审技能用 `jq -nc` 逐行序列化任务清单（JSONL），供 `/autoplan` 跨轮次聚合。**规格明令不得手写 JSONL**——手拼 JSON 遇到引号、换行、反斜杠就会产出坏文件，而坏文件在聚合端看起来像合法数据。本机没有 `jq`，所以 `/plan-ceo-review`（2026-10-03）的 21 条 Implementation Tasks **没有产出 JSONL**，那次评审不进 autoplan 聚合。

**Context:** 装好后可重跑那次评审的 JSONL 产出步骤补上。**不影响任何实验结果**，只是少一份跨轮次数据。同类工具缺口见下一条。

**Effort:** S
**Priority:** P3
**Depends on:** None

**Completed:** 2026-11-09，系统已有 `/usr/bin/jq`。

---

### ~~安装 Codex CLI，让评审有跨模型覆盖~~（已完成）

**What:** `npm install -g @openai/codex`，使 `/plan-ceo-review` 与 `/plan-eng-review` 的 Outside Voice 步骤走真正的跨模型评审，而不是回退到 Claude 子代理。

**Why:** Outside Voice 的设计意图是**用另一个厂商的模型独立审一遍**——不同模型的盲区不同，这是抓出「自己看不见的错误」最有效的手段。`/plan-ceo-review`（2026-10-03）返回 `CODEX_MODE: not_installed`，因此回退到 Claude 子代理：**16 条发现与我自己的视角同源**。事后逐条核实发现其中 **3 条断言站不住**（最典型的是「B5 按构造恰好正确」——它默认了四档响应率的数值，而那恰是未决的 0.2(b) 问题）。**同源评审更容易放过这一类。**

**Context:** 本次回退是规范允许的，不是错误；记下来是为了避免下次误以为「已经跨模型验证过了」。**装它的最佳时机是第一次冒烟表读数之后**——那时做评审才又有实测数据可依（见下）。**注意与「停止评审」条件的关系**：`/plan-ceo-review` 设的条件是「门 0 通过之后、首次冒烟表之前不再开新评审轮」，所以在那之前装它是没有用的。

**Effort:** S
**Priority:** P3
**Depends on:** None

**Completed:** 2026-11-09，Codex CLI 已在当前 Codex 桌面环境提供。

---

## Completed

### 把版权材料与原型代码物理隔离，为将来开源做准备

**Completed:** /plan-ceo-review 2026-10-03（未发版，记 commit `b95aac3`）

**结论：从 TODOS 升级为 Stage 0 的交付物，并已于 2026-11-26 执行。** 版权论文、中译全文与导师报告已从当前树及全部 Git 历史移除，仓库随后转为 public；边界见 `docs/PUBLIC_RELEASE.md`。

**为什么提前做：** `CLAUDE.md` 已把「将来开源必须先剥离」写成硬约束，而**没有任何机制保证它会发生**。本条自己记着「**现在 `src/` 还是空的，拆分成本最低**」——一条自己论证「越早越便宜」的推迟项，与「推迟」这个动作本身矛盾。它被推迟的理由（「开源可能永远不发生」）只否定了**紧迫性**，没有否定**成本曲线**。

**最终落地：** 公开前已做全历史文件级审计与历史重写；受限路径和导师报告在本地与远端历史中均不再可达，`.gitignore` 阻止再次误提交。完整私有历史另存为离线 bundle。
