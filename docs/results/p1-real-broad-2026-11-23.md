# P1 broad 真实模型结果

日期：2026-11-23

状态：**broad 场景的 A1/A2/A3 真实模型矩阵与 B5 同场景对照已完成；
其余三个预注册场景、独立人工裁决和真实互联网外部效度仍未完成**

## 1. 这次到底跑了什么

本轮运行的是预注册场景 `broad`：

- Stage 2 `RouterShim`，不是真实互联网；
- 32 个前缀，每趟 1,000 次探测；
- 每 100 次探测做一次决策；
- A1/A2/A3 × seeds 0–29；
- 每条策略每趟 10 次决策，共 900 次决策；
- provider 为当前轮换后的 `deepseek-flash`；
- `temperature=0`、`top_p=1`、最大输出 1,024；
- Responses 请求关闭 reasoning；
- 实际模型调用 577 次，缓存命中 323 次；
- 墙钟 997.63 秒；
- 所有 90 条 Agent 行均耗尽 1,000 次预算，`charged=probes_run=1000`。

模型调用少于 900 次，是因为 A1 的 300 次决策和 A2 的 23 次决策命中了此前同
配置的缓存；这不是缺失数据。900 条决策已用同一缓存重放，900/900 命中，且
主端点、waste、报告、账本与 Stage 2 结果逐字段一致。

B5-closed 与冻结的 B5-open 另用 seeds 0–29 在同一 `broad` 配置运行；它们是
确定性基线，不调用模型。

## 2. 结果

| 行 | 平均 `true_routers` | 中位数 | 范围 | 平均 `aliased_waste_ratio` |
|---|---:|---:|---:|---:|
| A1 固定工具池 | 11.30 | 11 | 11–14 | 0.8000 |
| A2 运行期组合 | 28.67 | 28.5 | 24–34 | 0.2480 |
| A3 opaque 编码 | 25.83 | 25 | 19–36 | 0.2467 |
| B5-closed | 56.60 | 57 | 52–63 | 0.2048 |
| B5-open（冻结 `balanced`） | 55.77 | 56 | 50–61 | 0.2048 |

同 seed 配对 Wilcoxon，并对预注册的三项主比较做 Holm 校正：

| 比较 | 主端点平均差 | Holm p | 结论 |
|---|---:|---:|---|
| A2 − A1 | +17.37 | 5.26e-06 | A2 显著更好 |
| A2 − B5-open | −27.10 | 5.26e-06 | A2 显著更差 |
| B5-open − B5-closed | −0.83 | 0.1939 | 无显著差异 |

次要端点 `aliased_waste_ratio` 与主端点方向一致：A2 比 A1 低 0.552；
A2 比 B5-open 高 0.0432。三个 Agent 行的 `aliased_caught` 均为 8，没有差异。

A3 相对 A2 少 2.83 个路由器，配对 Holm p=4.08e-05。这个结果只能说明本轮
opaque 编码条件下的差异，不能单独归因于“IPv6 知识被移除”，因为 A3 还有下面
第 4 节记录的输出形态兼容问题。

## 3. 当前可以说什么

在 `RouterShim` 的 `broad` 测试床和本轮模型配置下，可以回答：

1. **开放动作空间有效。** A2 比只允许单前缀探测的 A1 多发现 17.37 个路由器，
   浪费率低 55.2 个百分点，且 caught 不下降。
2. **Agent 的运行时组合优于固定工具池，但不优于同能力的人工冻结程序。**
   B5-open 与 A2 都拥有安全 DSL 的开放分配能力；人工在独立 held-out 拓扑上
   冻结的 `balanced` 程序显著胜过 A2。
3. **当前证据不支持“LLM Agent 比人工写了同能力程序更好”。** 它支持的是
   “开放空间比封闭空间更好”，同时把收益主要归给开放动作空间。

这三点只对 `broad` 场景、`RouterShim` 和本轮模型/provider 配置成立。它们不是
四场景总体判决，也不是真实 IPv6 互联网结论。

## 4. 证据限制

### 只有一个预注册场景有真实模型结果

`prefix_diversity`、`sparse_trap`、`hot_hierarchy` 仍只有 fixture 工程结果。
预注册协议要求正式矩阵覆盖四个场景，所以现在不能宣布总体 P1 判决。

### A3 请求 schema 有兼容偏差

A3 的 300 条决策中，30 条返回带 `action=construct` 的完整对象，270 条只返回：

```json
{"target": "balanced", "fraction": 0.2}
```

运行时把 step-only 形态明确解释为“一步、花完当前 epoch 的 compose”，并把
fraction 归一化为 1。它没有改变探测预算或安全边界，但偏离了原始动作外层
schema。因此本轮 A3 应作为兼容性敏感度结果；确认性的知识消融最好在固定 prompt
或 provider schema 后重跑。

### 缺少 token 与费用账本

当前能给出准确模型调用次数，但本轮运行时 Responses 适配器还没有启用 token
账本，也没有 provider 账单快照。后续入口现已支持可选的 `--usage-log` JSONL，
只保存模型名与 token 数；但本轮没有这些字段，不能倒推。

若论文要比较“人工构造”与“Agent 构造”的投入，后续正式运行必须启用 token
账本，并补齐 provider 费用和人工工时，不能只报调用次数。

### 缺少独立裁决

`adjudication.json` 仍是 `pending-human-adjudication`。A2 构造物与 B5-open 的
对等投入、安全检查和任务有效性必须由非场景作者按同一评分表先盲评再解盲。

### 不提供真实网络外部效度

`RouterShim` 的间接响应、路由器数量和 aliasing 规则都是显式测试床定义。
因此本次只能证明策略在该 Response 层测试床上的相对表现，不能证明在真实 IPv6
互联网上同样有效。

## 5. 证据位置

原始产物按项目约定放在被忽略的 `docker/run/`，本地 SHA-256 为：

| 产物 | SHA-256 |
|---|---|
| `p1-live-broad.json` | `f7af629c24206b2730d7b0459e6beee0da551618b90242c85c4fd9e4e1295674` |
| `p1-live-broad-cache.jsonl` | `e600f2e979db4d9b55a81b5dff71ce79766ffcd6d42e5878e3cff09354830fa4` |
| `p1-live-broad-replay.json` | `e38374babf63814100b549c7ba5f34294c2b2f05025a7b06bb5c671ce236e481` |
| `p1-live-broad-b5.json` | `b1948b593927d0508aef415a917b77c11ed50599283f5b34eadfdd71a89003b0` |
| `p1-live-broad-evidence.json` | `11def0367eae10a919e0306781c1904d977a922b0b5c364b81c60019adced973` |

复现后可用以下命令重新组装证据：

```bash
make p1-live-replay
make p1-live-b5
make p1-evidence
```

`p1-evidence` 会生成 `docker/run/p1-analysis.json`、
`docker/run/p1-live-broad-evidence-audit/` 和 900 条决策审计记录。审计汇总应为：
577 次模型调用、323 次缓存命中、600 次构造/组合尝试。
