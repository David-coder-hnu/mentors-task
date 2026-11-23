# P1 开放动作空间实验预注册

## 目的

回答两个独立问题：

1. 运行期构造新的探测程序，是否优于固定单步工具池？
2. 如果有效，LLM 的构造机制是否比人手写但能力对等的 B5-open 更有价值？

## 当前“构造”的定义

A2/A3 不执行任意 Python，而是提交一个安全 DSL 程序：

```json
{
  "action": "compose",
  "name": "adaptive_mix",
  "steps": [
    {"target": "unprobed", "fraction": 0.5},
    {"target": "responsive", "fraction": 0.5}
  ],
  "count": 100,
  "reason": "..."
}
```

可用 target：

- `balanced`：跨前缀轮转；
- `least_probed`：优先真实探测次数最少的前缀；
- `unprobed`：优先从未探过的前缀；
- `responsive`：优先 Beta(1,1) 平滑响应率最高的前缀。

限制：

- 最多 4 步；
- fraction 均在 `(0, 1]` 且总和为 1；
- 每步分配精确计入 runner 侧预算；
- 程序由可信解释器执行，策略进程不直接执行生成代码；
- schema 见 `config/protocols/program_schema.json`。

因此本文只能说“Agent 构造了新的组合程序”，不能说“Agent 执行了任意生成代码”。

## 预注册场景

manifest：`config/p1/manifest.json`。

| ID | 前缀 | 预算 | 目的 |
|---|---:|---:|---|
| broad | 32 | 1000 | 宽任务范围下的覆盖效率 |
| prefix_diversity | 21 | 1000 | aliased、低响应、稀疏和热点并存 |
| sparse_trap | 24 | 600 | 紧预算下陷阱惩罚 |
| hot_hierarchy | 21 | 800 | 真实多样性与单来源饱和响应混杂 |

四组场景在正式运行前固定。不得根据模型结果删除场景。

## 行和比较

| 行 | 定义 |
|---|---|
| A1 | 固定工具池，每 epoch 只能选一个前缀探测 |
| A2 | 同一初始池，可运行期 `compose` |
| A3 | 与 A2 同动作空间，只把前缀、状态和 region 替换为 opaque token |
| B5-closed | 6SEVEN 固定 dealiasing + bit-reversal 发现 |
| B5-open | 与 A2 相同 DSL，程序在评测前由人构造并冻结 |

主比较：

- `A2_vs_A1`：开放程序是否有效；
- `B5_open_vs_B5_closed`：人写的开放程序是否同样有效；
- `A2_vs_B5_open`：LLM 与人手构造机制在同一开放空间中谁更好。

主端点为 `true_routers`，次要端点为 `aliased_waste_ratio`。`aliased_caught`
用于诊断，不单独宣布胜负。

## 2×2 判决

| | B5-open 成功 | B5-open 失败 |
|---|---|---|
| **A2 成功** | 开放空间有效，但 LLM 无额外价值 | 支持 P1 |
| **A2 失败** | P1 被杀死 | 场景无区分力或开放空间无价值 |

“成功”要求 `true_routers` 相对各自封闭对照显著提高。若主端点不变但 waste
显著下降，只能判为效率优势，不能判 P1 成立。

## 场景级门槛

- 每个场景至少 30 个配对 seed；
- 同一 `(config, seed)` 单位；
- 比较使用配对 Wilcoxon 和 Holm；
- 所有 Agent 行必须恰好耗尽预算；
- 正式模型运行的决策缓存重放必须 100% 命中；
- B5-open 的工具池、构造次数 `K`、输入信息、验证器和裁决表与 A2 相同。

## 功效敏感度

`config/p1/power.json` 预先写死：

- `N = 30`；
- `true_routers` MDE = 5，保守配对差 SD 上限 = 8；
- `aliased_waste_ratio` MDE = 0.05，SD 上限 = 0.08；
- α = 0.05，目标功效 = 0.8。

当前 Monte Carlo Wilcoxon 敏感度结果均为 **0.888**，高于 0.8。模型 pilot 若观察到
更大的配对差 SD，必须在正式表前重算 N，不得沿用 30。

## 当前状态

工程 runner、compose DSL、B5-open 和 4 场景 fixture 已实现并跑通。
fixture 结果只用于验证执行与统计链路，不进入判决。

正式运行仍缺：

- 冻结模型身份与费用上限；
- B5-open 在留出拓扑上的参数/程序冻结记录；
- 非场景作者对 A2/B5-open 构造物的独立裁决。
