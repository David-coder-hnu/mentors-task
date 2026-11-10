# T16 工程闭环：A1/A2/A3 同 seed 配对预检

日期：2026-11-10

状态：**工程链路完成；真实模型结果仍待新的成本上限与凭据**

## 1. 本轮修复

此前 A2 在预算耗尽后还要调用一次模型生成 `final_report`，真实 provider 多次超过
300 秒，导致完整运行无法结束。

现在最终 aliased 判定改为 A1/A2/A3 共用的确定性规则：

- 至少探 2 次；
- 一次回包都未漏；
- 未探过的前缀不作声明。

模型只负责“探哪个前缀”或“构造怎样的分配”。这消除了最终声明的第二次模型调用，
也让三个 Agent 行的报告机制完全相同：`src/agent/reporting.py`。

## 2. A1/A2/A3 轨道

- A1：固定工具池；只有 `probe`；
- A2：同一初始池，额外允许 `construct` / `balanced_probe`；
- A3：与 A2 相同的开放动作空间，但前缀、状态和动作 region 全部使用 opaque token；
- 三行共同使用 Stage 2 `RouterShim`、相同预算、epoch、模型身份和评分器。

分析入口：`scripts/analyze_ablation.py`。

## 3. 30-seed 工程预检

为验证完整矩阵、缓存和配对统计，不在正式结果里冒充模型，本轮使用仓库内
`tests/fixtures/deterministic_agent_completion.py`：

- 3 行 × 30 seeds × 1,000 探；
- 90 趟全部完成，预算均恰好耗尽；
- 900 次决策首次全部写入缓存；
- 无 completion 回放 **900/900 命中**；
- A3 与 A2 在相同 seed 上逐项一致，说明 opaque 编解码没有改变动作执行；
- `A3_vs_A2` 在 `true_routers`、waste、caught 上均无可区分差异
  （成对检验 `p=1`，这是确定性 fixture 的预期）；
- `A2_vs_A1` 的差异来自 fixture 的固定策略，只证明统计链路能区分两种动作，
  **不证明 P1**。

工程预检产物在 `docker/run/`，被 `.gitignore` 排除；分析报告的 provenance 会显式
写成 `fixture`，并在开头标记“不是模型结果”。

## 4. 为什么仍不能把 T16 标为完成

正式 T16 要求真实 A1/A2/A3 的 30-seed 配对。当前不能直接开跑有两个外部原因：

1. 本机 provider 凭据在之前审计中曾出现在会话输出里，应先轮换，不能继续用于
   900 次正式调用；
2. `config/stage2_config.json` 的 `api_cost_cap=0`，而完整矩阵需要 900 次模型
   决策。没有用户给出新的成本上限，就不能自行产生这笔费用。

因此当前已完成的是：

- A3 运行轨道；
- hybrid 报告机制；
- 全会话预算与缓存；
- 同 seed 配对分析器；
- 30-seed 工程预检。

仍未完成的是：用新凭据和明确成本上限跑真实 A1/A2/A3 表。

## 5. 正式开跑条件

需要提供：

- 轮换后的 completion provider 或命令；
- `api_cost_cap` 的允许值；
- 选择冻结的模型身份与调用日期。

然后执行：

```bash
.venv/bin/python -m src.experiment --stage 2 --seeds 0-29 \
  --strategy src.agent.strategies:run_a1 \
  --strategy src.agent.strategies:run_a2 \
  --strategy src.agent.strategies:run_a3 \
  --completion-command '<new-provider-command>' \
  --decision-cache docker/run/stage2-a123-live-cache.jsonl \
  --out docker/run/stage2-a123-live.json

.venv/bin/python scripts/analyze_ablation.py docker/run/stage2-a123-live.json
```
