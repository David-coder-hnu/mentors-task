# docs/ — 索引

本目录收纳除设计文档与 `TODOS.md` 之外的全部文档。分成六层，**按权威性从高到低排列**：

| 目录 | 权威性 | 说明 |
|---|---|---|
| 仓库根 `../IPv6_Agent_探测_设计方案.md` | **唯一权威**（活文档） | 不在本目录，但它是所有冲突的终审。本目录任何文件与它冲突，以它为准 |
| `restricted/` | 原文 | 版权材料。**不得离开本仓库**，见下方硬约束 |
| `reports/` | 原文 | 自己写的调研报告。历史产出，不再更新 |
| `results/` | 实测结果 | 冻结的实验产出、统计分析与解释边界 |
| `protocols/` | 操作性协议 | T19 对等投入与 T14 评分门槛；由设计文档授权并用于审计 |
| `plans/` | 快照 | 评审产出的计划快照。**不是活文档**，价值在"当时为什么这么改" |
| `artifacts/` | 快照 | gstack 各技能的原始产出。同上，**都不是活文档** |

> **一句话规则**：`plans/` 与 `artifacts/` 里的任何判断，只要与根目录的设计文档不一致，都是**前者过时**，不是后者错了。它们是决策的**过程记录**，不是决策本身。

---

## `restricted/` — 版权材料（不得外流）

| 文件 | 说明 |
|---|---|
| `6Seeks_A_Global_IPv6_Network_Periphery_Scanning_System.pdf` | IEEE ToN 2025 原文，关键基线论文 |
| `6Seeks_中文全文.md` | 6Seeks 全文中文译读（含表 I–VI、图 1–16 图说、关键数字速查） |

**硬约束**：本仓库必须保持 private。转 public 会构成再分发 IEEE 版权内容。将来要开源原型代码时，必须先把本目录整体剥离（拆仓库或调整 `.gitignore`），**不能整仓库转 public**。

这条规则**有执行机制，不靠人记忆**：导出脚本按排除清单产出 `src/` 的干净副本，并**断言副本不含本目录下任何路径**。见设计方案 `## Constraints` 段（Stage 0 交付物，门 0 条件 9）。

**消费者**：人（阅读）、Stage 0 的导出脚本（作为排除清单的来源）。

---

## `reports/` — 自己写的调研报告

| 文件 | 说明 |
|---|---|
| `IPv6_Agent_Probing_Research_Report.pdf` | 调研报告正文（2026-09），含 6Gen→TNet 的相关工作演进与建议架构 |
| `IPv6_Agent_Probing_Research_Report.md` | 上述报告的 markdown 转写（原文中文，逐页对照页面图像核对） |

**权威性**：低于设计文档。它是设计文档的**前身**——设计文档 v1 由 `/office-hours` 基于它产出，此后两轮对抗式评审已把其中多处判断改写（最典型的是"贡献是 Agent 决策层"被改写为"贡献是靶场 + 可回放评分器"）。**引用它之前先确认设计文档没推翻相关结论。**

**消费者**：人（写论文时的相关工作段落素材）。

---

## `results/` — 实跑结果

| 文件 | 说明 |
|---|---|
| `stage1-2026-11-10.md` | Stage 1 正式 30-seed 真靶场结果、配对检验与解释边界 |
| `stage2-2026-11-10.md` | Stage 2 RouterShim 六行 × 30 seeds 基线、A1/A2/A3 接线状态与未完成边界 |
| `t7-6sense-compat-2026-11-10.md` | 本机 arm64 兼容的 6Sense `aliasv6 + scanv6` 去别名化验收 |
| `t16-engineering-2026-11-10.md` | A1/A2/A3 同 seed 配对工程闭环、30-seed fixture 预检与正式开跑条件 |
| `p1-engineering-2026-11-23.md` | A2 运行期组合程序、B5-open、四场景和 P1 审计包 |

结果文件只陈述对应 config、seed 和靶场下实际测到的内容，不把 Stage 1 结果外推到
P1、Stage 2 或真实互联网。

---

## `protocols/` — 已预注册的操作协议

| 文件 | 说明 |
|---|---|
| `equal_effort.md` | A2 与 B5-open 的同预算、同信息、同工具池、同裁决人协议 |
| `scoring_admissibility.md` | aliased 声明的证据门槛与精度门槛 |
| `b5_6seven_classification.md` | 6SEVEN online alias detection 的源码归类与 B5 构造方式 |
| `ablation.md` | A3 不透明同构编码、配置审计与当前阻塞边界 |
| `p1_preregistration.md` | P1 开放程序 DSL、四场景、A2/B5-open 比较与 2×2 判决 |

它们低于根设计文档，但高于 `plans/` 与 `artifacts/` 中的过程记录。正式实验若违反
这里的预注册规则，必须在结果中显式标记为偏离，不能静默修改门槛。

---

## 测试覆盖索引

`test_coverage.md` 把设计文档点名的 31 条路径映射到可执行测试。它是 T8 的
验收索引，不声称替代行覆盖率工具。

---

## `plans/` — 评审计划快照

| 文件 | 说明 |
|---|---|
| `2026-10-03-ceo-plan.md` | `/plan-ceo-review` 的快照，`status: PROMOTED` |

**权威性**：不是活文档。**所有判断已落进根目录的设计文档**。它的价值在**"当时为什么这么改"**：被否决的备选、被撤销的扩张、Outside Voice 16 条里没通过核实的 3 条。文件开头有完整说明。

**注意编号差异**：本文件的实现任务用 `CE1–CE21`；设计文档用 `T1–T19`。两套编号**不一一对应**（快照入库时做过重排）。要对齐任务，以设计文档为准。

**消费者**：人（追溯决策沿革）、未来的评审轮（避免重复已否决的提案）。

---

## `artifacts/` — gstack 技能原始产出

这些是 gstack 各技能写在 `~/.gstack/projects/David-coder-hnu-mentors-task/` 下的原始文件，**原样复制**进来（原处保留，gstack 仍在读写）。文件名前缀日期即产出日期。

| 文件 | 来源技能 | 权威性 |
|---|---|---|
| `2026-10-03-office-hours-design.md` | `/office-hours` | **已被设计文档取代**。设计文档 v1 的原始形态，含未被采纳的早期方案 |
| `2026-10-03-ceo-plan-original.md` | `/plan-ceo-review` | `plans/` 那份的**上游原版**（`T1–T21` 编号）。与 `plans/` 版本有 26 行差异，不是重复文件 |
| `2026-10-03-eng-review-test-plan-superseded.md` | `/plan-eng-review`（第一次） | **已作废**。两处数字是错的，见下一行文件开头的更正说明 |
| `2026-10-08-eng-review-test-plan.md` | `/plan-eng-review`（第三次，v6） | **当前有效的测试计划**。含对上一版两处数字错误的逐条更正 |
| `review-log.jsonl` | gstack 评审台账 | 5 条记录。机器可读，供 `/autoplan` 跨轮次聚合 |
| `learnings.jsonl` | `gstack-learnings-log` | 2 条记录。**注意**：本机此前的 learnings 曾因路径 bug 静默丢失，见下 |

**未收录的 gstack 文件及其原因**：

- `repo-mode.json`、`brain-cache/` —— gstack 的**内部运行状态**，不是文档。收录进来会给人一种"这是产出"的错觉。
- `decisions.jsonl` —— 曾被 `gstack-decision-log` 误写到 `~/.gstack/projects/**unknown**/`（`resolveSlug()` 在 Windows 下的路径 bug）。**该 bug 尚未修复**，所以没有可信的 decisions 文件可归档。

**消费者**：人（追溯 gstack 各轮评审的原始输出）、`/autoplan`（读 `*.jsonl`）。

---

## 已知缺口

- **`learnings.jsonl` 只有 2 条，不代表只学到 2 件事。** `gstack-learnings-log` 在 Windows 上曾因 bun 无法解析 MSYS 路径而**静默丢弃每一次写入**（`2>/dev/null` 吞掉了 `Cannot find module`）。该 bug 已于 `2026-10-08` 修复（gstack 仓库 commit `1ca69f54`），但**修复前丢失的数据不可恢复**。
- **`decisions.jsonl` 缺失**，原因是上面那条未修的 `unknown/` slug bug。
- **`plans/` 缺 `/plan-eng-review` 的快照。** eng review 的产物是测试计划，已收在 `artifacts/` 下；它不像 CEO review 那样产出一份独立计划文档。
