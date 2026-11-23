# mentors_task

**把 IPv6 主动探测里「该探哪个、探多少、用什么探」这一层决策从写死的算法里拿出来，交给一个能自己选策略的 Agent —— 但先回答一个更硬的问题：怎么证明它真的更好。**

导师课题的研究仓库。仓库里同时放着**调研/设计文档**与**在可控靶场上跑对比实验的原型代码**。

> ### ⚠️ 当前状态：Stage 1、Stage 2 基线与 T7 兼容轨道均有实测；P1 已完成一个真实模型场景，但**尚未完成四场景正式检验**
>
> - 20 个实现任务完成 **19** 个（T1–T15、T17–T20）—— 完成的是**靶场适配层、真值层、评分器、决策缓存、执行沙箱、实验编排层、留出集、RouterShim、统计层、在线目标、B0–B5 基线、测试覆盖索引、Stage 1 真靶场冒烟入口、记忆族与 T7 去别名化兼容轨道**。协议已升到 **v4**：Stage 1 五行、Stage 2 六条静态基线、A1/A2/A3 的 runner 侧决策代理与缓存回放都已接线。
> - **Stage 1 正式 30-seed 表已完成**：45,000 探、账本 `captured=enqueued=45,000`、零 libnet 错误。M2 相对 B4/M1 的 aliased waste 增加约 22.5 个百分点（Holm 校正后 p≈5.4e-6），caught 没有改善；当前 UCB1 跨 run 记忆设计是负面结果。完整结果见 `docs/results/stage1-2026-11-10.md`。
> - **Stage 2 RouterShim 六行基线已完成**：180,000 探、30 seeds；B5 平均发现 56.6 个路由器，B2/B4 约 29 个。A1/A2/A3 已接通协议 v4、runner 侧决策缓存、安全复合原语与共用确定性报告；30-seed fixture 预检和 900/900 缓存回放通过。完整基线结果见 `docs/results/stage2-2026-11-10.md`。
> - **T7 已在本机完成**：Linux/arm64 Docker 中原生编译锁定版本的 `aliasv6 + scanv6`；4/4 aliased seed 被拦截，进入 scanner 的 aliased 地址为 0。完整 LSTM 6Sense 仍不冒充已完成，范围见 `docs/results/t7-6sense-compat-2026-11-10.md`。
> - **P1 的 `broad` 场景已有真实模型结果**：A1/A2/A3 × 30 seeds 共 900 次决策，实际模型调用 577 次、缓存命中 323 次，900/900 缓存重放一致。A2 平均发现 28.7 个路由器，显著优于 A1 的 11.3；但冻结的人工 B5-open 平均 55.8，显著优于 A2。当前结果支持“开放动作空间有效”，不支持“Agent 比同能力的人工组合更有优势”。另外三个预注册场景、独立裁决和真实互联网验证仍缺。见 `docs/results/p1-real-broad-2026-11-23.md`。
> - **P1 总体仍是未完成的假说。** 一个 `RouterShim` 场景不能回答四场景总体、真实网络外部效度或完整知识消融。
> - 所有已发表的、写进设计文档的判断，凡涉及"Agent 会赢/不会赢"，都是**设计选择**，不是测量结果。

---

## 一、目前可以回答什么

项目目前能够支持的结论是：

> **已经有一套可运行、可回放、可证伪的 IPv6 探测评测台；受控实验表明，追高响应率的自适应策略会陷入“响应很多、实体很少”的 aliasing 陷阱，而当前跨运行记忆设计会进一步放大浪费。开放动作空间在模拟测试床上有效，但不能由此声称 Agent 优于同能力的人工组合。**

| 研究问题 | 当前答案 | 证据边界 |
|---|---|---|
| 只用回包数量指导预算会怎样？ | B2/B4 持续投入 aliased 前缀，只发现约 29 个路由器，waste 约 58% / 51% | RouterShim 合成测试床 |
| FRP-aware 手写策略是否有效？ | B5 在该测试床平均发现 **56.6** 个路由器，waste **20.5%**，显著优于 B2/B4 | 只在该 shim 定义下成立 |
| 加记忆是否自然改善探测？ | 当前 UCB1 跨 run 记忆是负面结果：M2 的 waste 比 B4/M1 高约 **22.5 个百分点**，caught 没改善 | Stage 1 真 Punching Bag，45,000 探 |
| “响应多”是否等于“真实实体多”？ | 不等；重复 aliased 来源不会重复计入 `true_routers` | Stage 2 评分定义与结果 |
| 6Sense 能否拦截已知 aliased 地址？ | 兼容轨道中 4/4 aliased seed 被拦截，进入 scanner 的量为 **0** | `aliasv6 + scanv6`，非完整 LSTM |
| 开放动作空间是否更好？ | 在 `broad` RouterShim 场景中有效：A2 平均 28.7 个路由器，A1 为 11.3 | 仅一个预注册场景；其余三个未跑 |
| LLM Agent 是否优于同能力人工程序？ | **当前不支持**：冻结 B5-open 平均 55.8，显著高于 A2 | 同一 `broad` 场景；仍需独立裁决 |
| 结论能否外推到真实互联网？ | **不能**；Stage 2 与 6Sense 兼容轨道都不提供外部效度 | P4 |

因此可以写“评测台有效、aliasing 陷阱可复现、当前记忆设计失败”；**不能写“Agent 已优于 RL”或“LLM 已学会主动构造探测原语”**。

完整结果入口：

- [Stage 1 正式结果](docs/results/stage1-2026-11-10.md)
- [Stage 2 RouterShim 结果](docs/results/stage2-2026-11-10.md)
- [T7 6Sense 兼容轨道](docs/results/t7-6sense-compat-2026-11-10.md)
- [T16 A1/A2/A3 工程预检](docs/results/t16-engineering-2026-11-10.md)
- [P1 broad 真实模型结果](docs/results/p1-real-broad-2026-11-23.md)
- [P1 开放动作空间工程闭环](docs/results/p1-engineering-2026-11-23.md)

## 二、问题出在哪

导师给的调研报告里有一句准确的 Gap：

> **关键 Gap**：现有系统越来越善于"动态调整一个固定算法"，但仍缺少一个能够根据测量目标、当前状态和历史经验，自主选择、组合并切换不同探测策略的**统一决策层**。

拆开看是三层：**发什么包**、**怎么收回声**——这两层是 IPv6 测量领域二十年的工程积累；而**该发哪种包、发到哪、发多少**，目前由人预先写死。Agent 要占的就是这一层。

两个术语必须先钉死，否则整个指标层是错的：

| 术语 | 含义 |
|---|---|
| **Indirect response（间接响应）** | 探测一个不存在的地址，收到 ICMPv6 Destination Unreachable，而响应的**源地址是不曾寻址的最后一跳路由器**。IPv6 外围探测的主流信号源 |
| **Aliasing / FRP（全响应前缀）** | 某个前缀下**每一个**地址都响应。它伪装成"这里很丰富"，实际信息量为零。AddrMiner 数据集里 **73.9%** 是 aliased |

aliasing 是这件事的难点：一个只会数"收到了多少回包"的系统，会把 aliased 前缀当成最大发现。

## 三、四个命题，以及它们各自的状态

| | 命题 | 状态 |
|---|---|---|
| **P1** | 区别在于**动作空间是开放的还是封闭的**，不在于「LLM vs RL」 | **待证伪的假说。** 原始表述曾写作「LLM vs RL」，被用户否掉后重写：真正要测的是「**允许构造新原语 vs 不允许**」，而 **A1（封闭工具池）vs A2（可构造原语）的差就是 P1 的全部内容** |
| **P2** | P1 目前只是假说，必须变成可测的量才能写进主张 | 采纳。若数据显示 Agent 在封闭工具池上**并不**比 RL 好，那是**有价值的负结果** |
| **P3** | 真正的贡献是 **ground-truth oracle + 可回放的评分器** | **已拆成两半，一半被证伪。** 见下 |
| **P4** | 测试床的合法性优先于规模；**仿真不提供外部效度** | 采纳。每个后端"能证明什么、不能证明什么"已逐条写进设计文档 |

**P3 为什么被拆开**：设计阶段发现 [**Punching Bag**](https://github.com/tumi8/punching-bag)（IMC 2026）**本身就是一个 ground-truth IPv6 靶场**——单机上按前缀配置响应率、能伪造 aliased 前缀、确定性可复现，并已用它评测过 6 个已发表的 TGA。所以"自建靶场"这一半不成立；贡献重新划分为：

1. **靶场 + 可回放评分器**（复用 Punching Bag，不自建）
2. **Agent 是靶场上的被测对象**，不是贡献本身

## 四、三条决定了仓库长相的核心判断

**① 贡献是靶场与评分器，Agent 是被测对象。** 因为大家缺的不是"再多一个算法"，而是**一个知道正确答案的评测台**——所有人只能报 hit rate / 覆盖数，而 hit rate 在 aliased 前缀面前失效。

**② Stage 1 只检验 P1 的"封闭"那一半，重心在 Stage 2。**

| 阶段 | 靶场 | 覆盖 | 主指标 |
|---|---|---|---|
| **Stage 1** | Punching Bag **原样复用** | 响应率 / aliasing 的策略选择 | `aliased_waste_ratio`、`aliased_caught`；`info_gain` 为诊断量 |
| **Stage 2** | 在其上加 **Response 层 shim**（不 fork 包路径） | 外围 / 间接响应 | `true_routers`；aliased 端点与路由器熵为诊断量 |

Punching Bag 只回 ICMPv6 Echo Reply——**没有路由器、没有拓扑、没有 hop limit 语义**。所以在 Stage 1 里，策略唯一能决定的是"下一个探哪个前缀"，**这正是 P1 指控 RL 的那种封闭动作空间**。P1 的完整检验只能在 Stage 2 做。于是 Stage 1 不再是"完整交付物"，而是 **Stage 2 的 harness 前置**。

**③ 强制隔离靠进程边界 + 执行沙箱，不靠约定。** 见下面的硬约束。

## 五、仓库结构

```
IPv6_Agent_探测_设计方案.md   ★ 唯一权威的设计文档（活文档，永远以它为准）
TODOS.md                        推迟项清单
README.md                       本文件
CLAUDE.md                       给 AI 助手的协作约定

Makefile                        本地入口（门 1 条件 2 的 netns 半边；`make help`）

config/stage1_config.json       Stage 1 的配置规格 —— 同时就是 Stage 1 的真值
config/stage2_config.json       Stage 2 的预算/模型身份；拓扑由 RouterShim 固定生成
config/heldout/                 独立前缀空间与响应率分布的留出集真值（T18）
config/protocols/               对等投入与新原语 schema
src/
  adapter/                      靶场适配层：ProbeSurface / Oracle 分离，进程边界，执行沙箱
  baselines/                    B0–B5、M1/M2 基线与记忆族
  agent/                        A1/A2/A3、缓存、状态规范化、在线目标与 opaque 编码
  scoring/                      Stage 1/2 可回放评分、端点、统计与报告
  experiment/                   实验编排层：Stage 1/2 矩阵、决策代理与一键入口
docker/                         Punching Bag 靶场容器（锁定上游 commit）
scripts/                        吞吐标定、实验分析、6Sense 兼容轨道与 completion 适配器
tests/unit/                     单测（进 hosted CI）
tests/integration/              需要 Docker / CAP_NET_RAW 的集成测试（marker 排除）
docs/                           文档存档，见 docs/README.md
```

**`src/` 的四块不是随意分的**，它们对应两条绝不能混的职责：

- `ProbeSurface` / `Oracle` **必须分离**——前者是交给策略的唯一对象，后者只给 runner 与评分器用。
- 策略侧**只能看到 `ProbeSurface`**，不得触及任何真值字段。

**文档从哪读起**：先读 [`docs/README.md`](docs/README.md)，它按权威性给每个文件定位，并说明冲突时以谁为准。一句话规则：`plans/` 与 `artifacts/` 里的任何判断，只要与根目录设计文档不一致，**都是前者过时**——它们是决策的过程记录，不是决策本身。

## 六、怎么跑

零运行时依赖，只用 Python 标准库。开发依赖只有 `pytest`。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest

# Windows 使用：
# .venv\Scripts\python.exe -m pip install pytest
# .venv\Scripts\python.exe -m pytest

make test          # 当前宿主：721 passed, 7 skipped
```

Linux 容器全量测试包含真实 IPv6 集成测试，当前为 **728 passed**。

跑一次实验（一条命令，零参数即用 `config/stage1_config.json` 跑 B0）：

```bash
.venv/bin/python -m src.experiment --offline --seeds 0-2
```

重建留出集（固定 seed，产物可直接 diff）：

```bash
.venv/bin/python -m scripts.make_heldout
```

留出集位于 `config/heldout/stage1_heldout.json`，全部前缀落在独立的
`2001:db8:a5::/48`，aliased 数量与响应率分档都刻意不同于调参集。它用于调参，
不参与主表；对应断言在 `tests/test_heldout_disjoint.py`。

`--offline` 是**排练靶场**（不出网卡、只按 `aliased` 前缀合成回包），用来验证编排链路本身；**由它得出的数不进入报告**。去掉 `--offline` 走真靶场，但需要 Linux + `CAP_NET_RAW`。

- Stage 1 真靶场行：`B0` / `B1` / `B4` / `M1` / `M2`；
- Stage 2 RouterShim 静态行：`B0`–`B5`；
- Agent 行：`A1` / `A2` / `A3`，需要 runner 侧 completion 或已冻结的决策缓存。

M1 每趟冷启动；M2 读取并回写本策略自己的记忆，两行首 epoch 相同。

本地目标（`make help` 列全部）：

```bash
make test          # 单测（hosted CI 跑的就是这一条）
make heldout       # 重建 Stage 1 留出集（固定 seed）
make smoke-table SEEDS=0-2   # Stage 1 五行真靶场冒烟表
make analyze-stage1          # 对最近一次 Stage 1 表做配对检验与分布
make stage2-baselines        # Stage 2 六行 × 30 seeds（RouterShim，非外网）
make analyze-stage2          # Stage 2 逐 seed 分布与基线配对比较
make ablation-fixture        # A1/A2/A3 工程预检（fixture，不是模型结果）
make analyze-ablation        # A1/A2/A3 同 seed 配对与 provenance 标记
make p1-power                # P1 预注册功效敏感度
make tune-b5-open            # 在独立 held-out 拓扑上冻结 B5-open 程序
make p1-fixture              # 四场景 A1/A2/A3/B5-open/B5-closed 工程矩阵
make analyze-p1              # P1 开放动作空间矩阵分析
make p1-audit                # 构造、API 与 epoch 观测审计包
make p1-live-replay          # 无模型调用地重放已有 broad 真实决策
make p1-live-b5              # 在 broad 运行冻结 B5 两条确定性基线
make p1-evidence             # 组装 broad 主比较并重做分析/审计
make 6sense-compat           # T7：Linux 容器中的 aliasv6 + scanv6 去别名化验收
```

`make smoke-table` 现在会启动 Stage 1 Punching Bag、在容器内跑五行矩阵、**先停靶场再读账本**，最后把结果与账本写进 `docker/run/stage1-smoke-table.json`。正式全表使用 `SEEDS=0-29`。

集成测试需要 Linux + `CAP_NET_RAW` + 一个在跑的靶场，在非 Linux 宿主上直接跑会 **skip**——单测进 hosted CI，集成测试用 marker 排除。

```bash
# 靶场冒烟（正例 5/5 + 负对照 0/5，退出码 0 即通过）
cd docker
docker compose up --build --abort-on-container-exit --exit-code-from prober
docker compose down
```

## 七、两条硬约束（违反则整组实验作废）

1. **策略侧只能看到 `ProbeSurface`，不得触及任何真值字段。**
   强制手段是**进程边界**（策略跑在子进程，只经 stdio 拿 `Response`）**加上执行沙箱**（拒绝读 `config/`、拒绝 `subprocess`、拒绝直接网络访问）。**两条都有红灯测试守着，不是约定。**
2. **探测并发下 `state` 必须规范化**，否则决策缓存永不命中、实验不可复现。

## 八、使用限制与第三方许可 ⚠️

**本仓库必须保持 private。**

`docs/restricted/` 内含 **IEEE ToN 2025 论文原文 PDF 与全文中文译读**，转 public 会构成再分发版权内容。**将来若要开源原型代码，必须先把该目录整体剥离**（拆仓库或调整 `.gitignore`），**不能整仓库转 public**。这条规则有执行机制、不靠人记忆：导出脚本按排除清单产出干净副本，并**断言副本不含该目录下任何路径**。

**第三方组件**：

| 组件 | 许可 | 与本仓库的关系 |
|---|---|---|
| [Punching Bag](https://github.com/tumi8/punching-bag) | **AGPL-3.0** | `docker/` 在**构建期** clone，锁定 commit（写死在 `docker/punchingbag.Dockerfile` 并断言回读的 HEAD 相等）。**不 vendor 源码**。本地研究使用、不分发、不对网络提供服务 ⇒ **不触发义务**；**分发构建出的镜像、或走 fork 回退路线并分发** ⇒ 衍生作品须以 AGPL 发布 |
| 6Seeks（IEEE ToN 2025） | — | 只引用论文；它**不公开**发现的 1.28 亿个 IPv6 外围地址，只公开源码与活跃 /48 前缀 |

**本仓库自身尚无 `LICENSE` 文件，自有代码的许可证未定。** 上面两条是**独立**的约束，将来开源时两条都要过：一条管"别把付费论文传出去"，一条管"用了 copyleft 代码就得按 copyleft 走"。

第三方论文 PDF **不进版本库**。引用第三方数据/代码一律用「链接 + commit hash」，不 vendor 原文。

## 九、协作约定

- **设计文档是唯一权威**。任何判断上的冲突，以它为准；其余文档都是快照。
- **改代码要同时改文档**：设计文档里的 `` `:NNN` `` 是**它自己的行号**，`src/` 与 `tests/` 里有上百处引用。写回采用**零行号漂移**规矩——正文只做**同行**替换，新增内容整段追加到文末。
- 本仓库使用 [gstack](https://github.com/garrytan/gstack) 技能做评审与 QA，约定见 `CLAUDE.md`。

---

## 引用

若本仓库的方法或靶场设计对你有用：

- **Punching Bag** — Steger, Junginger, Carle, Zirngibl. *Punching Bag: A Ground-Truth Testbed for IPv6 Address Discovery.* ACM IMC 2026. DOI [`10.1145/3777912.3839826`](https://doi.org/10.1145/3777912.3839826)
- **6Seeks** — *A Global IPv6 Network Periphery Scanning System.* IEEE/ACM Transactions on Networking, 2025.
