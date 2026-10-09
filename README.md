# mentors_task

**把 IPv6 主动探测里「该探哪个、探多少、用什么探」这一层决策从写死的算法里拿出来，交给一个能自己选策略的 Agent —— 但先回答一个更硬的问题：怎么证明它真的更好。**

导师课题的研究仓库。仓库里同时放着**调研/设计文档**与**在可控靶场上跑对比实验的原型代码**。

> ### ⚠️ 当前状态：早期研究阶段，**没有任何实验结论**
>
> - 19 个实现任务完成 **7** 个（T1–T5、T10、T13）—— 完成的是**靶场适配层、真值层、评分器、决策缓存与执行沙箱**，即"能跑实验的地基"。
> - **Agent 是否更好，是一个尚未开始检验的假说。** 本仓库目前没有任何一行数据支持或反对它。
> - 所有已发表的、写进设计文档的判断，凡涉及"Agent 会赢/不会赢"，都是**设计选择**，不是测量结果。

---

## 一、问题出在哪

导师给的调研报告里有一句准确的 Gap：

> **关键 Gap**：现有系统越来越善于"动态调整一个固定算法"，但仍缺少一个能够根据测量目标、当前状态和历史经验，自主选择、组合并切换不同探测策略的**统一决策层**。

拆开看是三层：**发什么包**、**怎么收回声**——这两层是 IPv6 测量领域二十年的工程积累；而**该发哪种包、发到哪、发多少**，目前由人预先写死。Agent 要占的就是这一层。

两个术语必须先钉死，否则整个指标层是错的：

| 术语 | 含义 |
|---|---|
| **Indirect response（间接响应）** | 探测一个不存在的地址，收到 ICMPv6 Destination Unreachable，而响应的**源地址是不曾寻址的最后一跳路由器**。IPv6 外围探测的主流信号源 |
| **Aliasing / FRP（全响应前缀）** | 某个前缀下**每一个**地址都响应。它伪装成"这里很丰富"，实际信息量为零。AddrMiner 数据集里 **73.9%** 是 aliased |

aliasing 是这件事的难点：一个只会数"收到了多少回包"的系统，会把 aliased 前缀当成最大发现。

## 二、四个命题，以及它们各自的状态

| | 命题 | 状态 |
|---|---|---|
| **P1** | 区别在于**动作空间是开放的还是封闭的**，不在于「LLM vs RL」 | **待证伪的假说。** 原始表述曾写作「LLM vs RL」，被用户否掉后重写：真正要测的是「**允许构造新原语 vs 不允许**」，而 **A1（封闭工具池）vs A2（可构造原语）的差就是 P1 的全部内容** |
| **P2** | P1 目前只是假说，必须变成可测的量才能写进主张 | 采纳。若数据显示 Agent 在封闭工具池上**并不**比 RL 好，那是**有价值的负结果** |
| **P3** | 真正的贡献是 **ground-truth oracle + 可回放的评分器** | **已拆成两半，一半被证伪。** 见下 |
| **P4** | 测试床的合法性优先于规模；**仿真不提供外部效度** | 采纳。每个后端"能证明什么、不能证明什么"已逐条写进设计文档 |

**P3 为什么被拆开**：设计阶段发现 [**Punching Bag**](https://github.com/tumi8/punching-bag)（IMC 2026）**本身就是一个 ground-truth IPv6 靶场**——单机上按前缀配置响应率、能伪造 aliased 前缀、确定性可复现，并已用它评测过 6 个已发表的 TGA。所以"自建靶场"这一半不成立；贡献重新划分为：

1. **靶场 + 可回放评分器**（复用 Punching Bag，不自建）
2. **Agent 是靶场上的被测对象**，不是贡献本身

## 三、三条决定了仓库长相的核心判断

**① 贡献是靶场与评分器，Agent 是被测对象。** 因为大家缺的不是"再多一个算法"，而是**一个知道正确答案的评测台**——所有人只能报 hit rate / 覆盖数，而 hit rate 在 aliased 前缀面前失效。

**② Stage 1 只检验 P1 的"封闭"那一半，重心在 Stage 2。**

| 阶段 | 靶场 | 覆盖 | 主指标 |
|---|---|---|---|
| **Stage 1** | Punching Bag **原样复用** | 响应率 / aliasing 的策略选择 | 泛化后的 `info_gain`（实体 = 非 aliased 的响应前缀集合） |
| **Stage 2** | 在其上加 **Response 层 shim**（不 fork 包路径） | 外围 / 间接响应 | `info_gain = I(Probes ; Routers)` |

Punching Bag 只回 ICMPv6 Echo Reply——**没有路由器、没有拓扑、没有 hop limit 语义**。所以在 Stage 1 里，策略唯一能决定的是"下一个探哪个前缀"，**这正是 P1 指控 RL 的那种封闭动作空间**。P1 的完整检验只能在 Stage 2 做。于是 Stage 1 不再是"完整交付物"，而是 **Stage 2 的 harness 前置**。

**③ 强制隔离靠进程边界 + 执行沙箱，不靠约定。** 见下面的硬约束。

## 四、仓库结构

```
IPv6_Agent_探测_设计方案.md   ★ 唯一权威的设计文档（活文档，永远以它为准）
TODOS.md                        推迟项清单
README.md                       本文件
CLAUDE.md                       给 AI 助手的协作约定

config/stage1_config.json       Stage 1 的配置规格 —— 同时就是 Stage 1 的真值
src/
  adapter/                      靶场适配层：ProbeSurface / Oracle 分离，进程边界，执行沙箱
  baselines/                    基线策略（B0–B5）
  agent/                        Agent 决策层：状态规范化 + 决策缓存 + LLM 决策体
  scoring/                      可回放评分器
docker/                         Punching Bag 靶场容器（锁定上游 commit）
scripts/                        吞吐标定与 sweep
tests/unit/                     单测（进 hosted CI）
tests/integration/              需要 Docker / CAP_NET_RAW 的集成测试（marker 排除）
docs/                           文档存档，见 docs/README.md
```

**`src/` 的四块不是随意分的**，它们对应两条绝不能混的职责：

- `ProbeSurface` / `Oracle` **必须分离**——前者是交给策略的唯一对象，后者只给 runner 与评分器用。
- 策略侧**只能看到 `ProbeSurface`**，不得触及任何真值字段。

**文档从哪读起**：先读 [`docs/README.md`](docs/README.md)，它按权威性给每个文件定位，并说明冲突时以谁为准。一句话规则：`plans/` 与 `artifacts/` 里的任何判断，只要与根目录设计文档不一致，**都是前者过时**——它们是决策的过程记录，不是决策本身。

## 五、怎么跑

零运行时依赖，只用 Python 标准库。开发依赖只有 `pytest`。

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install pytest      # Windows
# .venv/bin/python -m pip install pytest            # Linux / macOS

.venv/Scripts/python.exe -m pytest                  # 452 passed, 7 skipped（2026-10-09）
```

集成测试需要 Linux + `CAP_NET_RAW` + 一个在跑的靶场，在宿主上直接跑会 **skip**（Windows 建不了 ICMPv6 原始套接字）——这正是设计要的语义：单测进 hosted CI，集成测试用 marker 排除。

```bash
# 靶场冒烟（正例 5/5 + 负对照 0/5，退出码 0 即通过）
cd docker
docker compose up --build --abort-on-container-exit --exit-code-from prober
docker compose down
```

## 六、两条硬约束（违反则整组实验作废）

1. **策略侧只能看到 `ProbeSurface`，不得触及任何真值字段。**
   强制手段是**进程边界**（策略跑在子进程，只经 stdio 拿 `Response`）**加上执行沙箱**（拒绝读 `config/`、拒绝 `subprocess`、拒绝直接网络访问）。**两条都有红灯测试守着，不是约定。**
2. **探测并发下 `state` 必须规范化**，否则决策缓存永不命中、实验不可复现。

## 七、使用限制与第三方许可 ⚠️

**本仓库必须保持 private。**

`docs/restricted/` 内含 **IEEE ToN 2025 论文原文 PDF 与全文中文译读**，转 public 会构成再分发版权内容。**将来若要开源原型代码，必须先把该目录整体剥离**（拆仓库或调整 `.gitignore`），**不能整仓库转 public**。这条规则有执行机制、不靠人记忆：导出脚本按排除清单产出干净副本，并**断言副本不含该目录下任何路径**。

**第三方组件**：

| 组件 | 许可 | 与本仓库的关系 |
|---|---|---|
| [Punching Bag](https://github.com/tumi8/punching-bag) | **AGPL-3.0** | `docker/` 在**构建期** clone，锁定 commit（写死在 `docker/punchingbag.Dockerfile` 并断言回读的 HEAD 相等）。**不 vendor 源码**。本地研究使用、不分发、不对网络提供服务 ⇒ **不触发义务**；**分发构建出的镜像、或走 fork 回退路线并分发** ⇒ 衍生作品须以 AGPL 发布 |
| 6Seeks（IEEE ToN 2025） | — | 只引用论文；它**不公开**发现的 1.28 亿个 IPv6 外围地址，只公开源码与活跃 /48 前缀 |

**本仓库自身尚无 `LICENSE` 文件，自有代码的许可证未定。** 上面两条是**独立**的约束，将来开源时两条都要过：一条管"别把付费论文传出去"，一条管"用了 copyleft 代码就得按 copyleft 走"。

第三方论文 PDF **不进版本库**。引用第三方数据/代码一律用「链接 + commit hash」，不 vendor 原文。

## 八、协作约定

- **设计文档是唯一权威**。任何判断上的冲突，以它为准；其余文档都是快照。
- **改代码要同时改文档**：设计文档里的 `` `:NNN` `` 是**它自己的行号**，`src/` 与 `tests/` 里有上百处引用。写回采用**零行号漂移**规矩——正文只做**同行**替换，新增内容整段追加到文末。
- 本仓库使用 [gstack](https://github.com/garrytan/gstack) 技能做评审与 QA，约定见 `CLAUDE.md`。

---

## 引用

若本仓库的方法或靶场设计对你有用：

- **Punching Bag** — Steger, Junginger, Carle, Zirngibl. *Punching Bag: A Ground-Truth Testbed for IPv6 Address Discovery.* ACM IMC 2026. DOI [`10.1145/3777912.3839826`](https://doi.org/10.1145/3777912.3839826)
- **6Seeks** — *A Global IPv6 Network Periphery Scanning System.* IEEE/ACM Transactions on Networking, 2025.
