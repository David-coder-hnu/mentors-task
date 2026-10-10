# mentors_task — Agent 驱动的 IPv6 主动探测调研

导师课题的研究仓库。研究目标：论证用 Agent / LLM 改造 IPv6 主动探测中「探测策略选择」的可行性，让 Agent 自主选择测量工具与策略，而不只是调某个固定算法的参数。

仓库**同时存放调研笔记与原型代码**（2026-10-03 起）：代码用于在可控靶场上跑 Agent vs 基线的可回放对比实验。此前"不是代码项目"的定位已作废。

## 文件

> **文档布局**（2026-10-08 收拢）：仓库根只留**活文档**（设计文档、`CLAUDE.md`、`TODOS.md`），其余全部归入 `docs/`。**先读 [`docs/README.md`](docs/README.md)** —— 它按权威性给每个文件定位，并说明冲突时以谁为准。

| 文件 | 说明 |
|---|---|
| `IPv6_Agent_探测_设计方案.md` | **唯一权威的设计文档（活文档，永远以它为准）**。2026-10-03 由 `/office-hours` 产出，经 3 轮对抗式评审 + **两次** `/plan-eng-review` 四段评审 + `/plan-ceo-review`（含 Outside Voice），**现为 v6**。核心决定：贡献是靶场 + 可回放评分器，Agent 是靶场上的被测对象。**v2**：发现 Punching Bag（IMC 2026）本身就是这类靶场，贡献拆两半。**v4**：P1 由「LLM vs RL」改写为「**开放 vs 封闭动作空间**」；Stage 2 由 fork 包路径改为 **Response 层 shim**；主终点由 `info_gain` 改为**可数量**；Stage 1 降为 Stage 2 的 harness 前置。**v5**：把 CEO review 的 22 条写回逐条落地（Stage 1 降为冒烟表、30 次比较的 Holm–Bonferroni、两个 co-primary 端点的判决规则、P1 的 2×2 杀死矩阵、A3 声明失效、缓存键加 config 哈希）。**v6**：第三次评审（`/plan-eng-review`，2026-10-08）验的是**「要求写了、交付物在不在」**——查出三处脱节并落地：族规模分母写死 **28**（不是 30，A3 已声明失效）+ 三级阶梯 28→30→36、新增 `T18` 交付**留出集**（门 2 条件 3 的原话是「不修就整个实验作废」却无任务交付它）、新增 `T6c` 补 `M1`/`M2` 的**行为定义**（冒烟表主比较的自变量原先只有半句标注）；critical gap 由 4 条涨到 **7 条且全部静默**。含 8 行基线表（B0–B5、A1–A3）、Stage 0–3 拆分、8 条 `Reviewer Concerns`、**T1–T20** 任务清单。**v9**（2026-10-10）：新增 **`T20`** —— 实验编排层（`src/experiment/`），把 CEO plan 的 **`C6`**「一键复现脚本」落成任务块（它进了门 1 的交付物清单与工时估算，却从来没有任何 T 编号）；并更正两处把「冒烟表真靶场实跑」误记为 `T16` 的地方（`:1517` 的 T16 是知识消融 A3，且当时 BLOCKED）。见文末 T20 完成块 |
| `TODOS.md` | 推迟项清单（2026-10-03 建立）。**当前 7 条**：导师答复补录（P1）、池大小上界变成会红的检查（P2）、其余 5 个 TGA 基线（P3）、Linux mount namespace 沙箱（P3）、安装 `jq`（P3）、安装 Codex CLI（P3）、fork 回退预案（P4）。原「版权材料隔离」已**关闭**——升级为 Stage 0 交付物（门 0 条件 9） |
| `docs/README.md` | **文档索引**。`docs/` 下分四层（`restricted/` 版权材料、`reports/` 调研报告、`plans/` 评审快照、`artifacts/` gstack 原始产出），按权威性排列；说明每个文件的用途与冲突裁决顺序 |
| `docs/restricted/` | **版权材料，仓库必须保持 private**：6Seeks IEEE ToN 2025 原文 + 中文全文译读。将来开源原型代码前必须整目录剥离，由导出脚本断言（门 0 条件 9） |
| `docs/reports/` | 自己写的调研报告（2026-09）PDF + markdown 转写。**是设计文档的前身**，多处判断已被评审改写，引用前先确认 |
| `docs/plans/2026-10-03-ceo-plan.md` | `/plan-ceo-review` 的快照（`status: PROMOTED`）。**不是活文档**——所有判断已落进上面的设计文档。**它的价值在"当时为什么这么改"**：被否决的备选、被撤销的扩张、Outside Voice 16 条里没通过核实的 3 条。**与设计文档冲突时以后者为准**（文件开头有完整说明）。注意任务编号是 `CE1–CE21`，与设计文档的 `T1–T19` **不一一对应** |
| `docs/artifacts/` | gstack 各技能的**原始产出**（office-hours 设计稿、两版测试计划、CEO plan 原版、评审台账 JSONL）。全部是过程记录，不是决策本身。已知缺口见 `docs/README.md` 末段 |

## 代码（原型）

- `src/` — 原型代码所在。五块：靶场适配层（**`ProbeSurface` / `Oracle` 必须分离**，见设计方案 D7）、基线策略集合（B0–B5）、Agent 决策层、可回放评分器、**实验编排层**（`src/experiment/`，T20：`策略 × seed` 的矩阵 + 一键入口 `python -m src.experiment`；它读真值，所以**在策略侧的禁止导入表里**）。
- **靶场路线已定稿**：Stage 1 复用 Punching Bag **原样**（不自建 `FakeNet`）；Stage 2 在其上加 `RouterShim`（Response 层合成，**不 fork 包路径**）。
- 原型代码的目标是**做实验**，不是做产品：优先保证可回放（固定 seed、确定性响应、**状态规范化**）与 ground truth 可导出。
- **两条硬约束**（违反则整组实验作废）：策略侧只能看到 `ProbeSurface`，**不得触及任何真值字段**；探测并发下 `state` 必须规范化，否则决策缓存永不命中。

## 约束

- **仓库保持 private**：内含 IEEE 版权论文原文与全文翻译，转 public 会构成再分发版权内容。**若将来要开源原型代码，必须先把版权论文 PDF 与全文译读从待公开的内容里剥离**（拆仓库或调整 `.gitignore`），不能整仓库转 public。**这条规则已有执行机制，不靠人记忆**：版权材料收进 `docs/restricted/`，由导出脚本按排除清单产出干净副本并**断言副本不含该目录下任何路径**——见设计方案 `## Constraints` 段（Stage 0 交付物，门 0 条件 9）。
- 6Seeks 论文**不公开**其发现的 1.28 亿个 IPv6 外围地址，只公开源码与活跃 /48 前缀。要做对比实验需先确认数据可得性，或自行复现。
- **第三方论文不进仓库**：新下载的论文 PDF 放临时目录，不进版本库。引用第三方数据/代码时用「链接 + commit hash」的方式，不 vendor 原文。
- 复用或改写第三方工具时，登记其许可证与上游 commit。**Punching Bag**（`github.com/tumi8/punching-bag`）：
  - **上游 commit（T1 锁定，2026-10-08）**：`cfe60b3e25a19113588cb5fe7313dd3da658d7ca`（`main`，提交日 2026-10-05）。写死在 `docker/punchingbag.Dockerfile` 的 `PB_COMMIT`，**构建期断言**回读的 HEAD 与它相等——不是注释里的一句话。
  - 许可证是 **AGPL-3.0**（上游 `LICENSE`，全文 661 行）。⚠️ **本文件此前记为 CC-BY 4.0，是错的**；上游仓库唯一的许可证文件就是 AGPL-3.0，源码文件无独立头注、README 无许可证字样。**这个差别是承重的**：CC-BY 只需署名，AGPL 是**强 copyleft 且带网络条款（§13）**。
    - **当前不受影响**：Stage 0/1 的本地研究使用不分发、不对网络提供服务，不触发任何义务。
    - **会触发的情形**：分发 `docker/` 构建出的镜像；或走 TODOS 的 **fork 回退路线**（P4）并分发。届时衍生作品须以 AGPL 发布。
    - **与前一条不是同一件事，别混**：「仓库保持 private」管的是*别把付费论文传出去*（再分发版权内容）；这一条管的是*用了 copyleft 代码就得按 copyleft 走*。**将来开源原型代码时两条都要过。**

## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
test -d ~/.claude/skills/gstack/bin && echo "GSTACK_OK" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use ~/.claude/skills/gstack/... for gstack file paths (the global path).

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
