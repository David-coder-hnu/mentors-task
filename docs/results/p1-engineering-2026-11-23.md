# P1 开放动作空间工程闭环

日期：2026-11-23

状态：**工程闭环已完成；broad 场景已有真实模型结果，其余三个预注册场景仍待运行。
真实结果与证据边界见
[`p1-real-broad-2026-11-23.md`](p1-real-broad-2026-11-23.md)**

## 1. 本轮新增

### 运行期组合原语

A2/A3 新增 `compose` 动作。模型提交的是一段有序程序，例如：

```json
{
  "steps": [
    {"target": "unprobed", "fraction": 0.5},
    {"target": "responsive", "fraction": 0.5}
  ],
  "count": 100
}
```

可信解释器支持 `balanced`、`least_probed`、`unprobed`、`responsive` 四种 step，
最多四步，fraction 总和必须恰好为 1。程序每 epoch 精确耗尽 runner 侧预算。

这比原来的 `balanced_probe` 前进了一步：A2 不是只选一个预编译动作，而是在运行时
组合新的探测程序。但它仍是安全 DSL，不是任意 Python 代码执行。

### B5-open

新增 `src/baselines/b5_open.py`：

- 检测阶段与 B5-closed 完全相同；
- 发现阶段执行一个在评测前冻结的手写 DSL 程序；
- 与 A2 使用相同 step 语义和预算账本。

程序选择只使用独立 held-out 拓扑 `config/heldout/stage1_heldout.json`：

- 7 个预注册候选；
- 10 个 tuning seeds；
- 选择规则为最大化 `true_routers`，再用 waste 和名称打破平局；
- 最终冻结 `balanced`：平均 52.4 个路由器、waste 14.87%；
- 冻结记录见 `config/p1/b5_open_frozen.json`。

### 四个预注册场景

- `broad`：32 前缀、1000 探；
- `prefix_diversity`：21 前缀、1000 探；
- `sparse_trap`：24 前缀、600 探；
- `hot_hierarchy`：21 前缀、800 探。

场景、比较、2×2 判决与功效门槛在
`docs/protocols/p1_preregistration.md` 中冻结。

## 2. 功效敏感度

预注册参数：

- `N=30`；
- `true_routers` MDE=5，保守配对 SD 上限=8；
- `aliased_waste_ratio` MDE=0.05，SD 上限=0.08；
- α=0.05，目标 power=0.8。

Monte Carlo paired-Wilcoxon 结果：

```text
true_routers:        power=0.8880 PASS
aliased_waste_ratio: power=0.8880 PASS
```

如果真实模型 pilot 的配对差 SD 更大，正式表前必须重算 N。

## 3. 30-seed 工程矩阵

使用确定性 fixture，不使用模型：

- 4 场景 × 30 seeds × 5 行；
- 600 次策略运行；
- 3,060 次 Agent 决策；
- A2/A3 每次决策都实际执行了 `compose`；
- 2040 条构造尝试、3060 条 API/决策审计记录已生成；
- 所有运行预算耗尽，账本一致。

fixture 的统计结果全部标记为 `fixture-only`，只用于工程验证：

- 四个场景都出现 `A2 beats A1`；
- B5-open 的冻结程序通常与 B5-closed 接近，但没有显著胜过；
- A2 与 B5-open 的相对结果随场景变化；
- A3 与 A2 的决策轨迹一致，验证 opaque 编解码不改变动作执行。

这些结果**不能**用于判定 P1。

## 4. 审计包

`scripts/make_p1_audit.py` 会生成：

- `tool_pool_manifest.json`
- `construction_attempts.jsonl`
- `api_usage.jsonl`
- `epoch_observations/<strategy>/<scenario>/<seed>.jsonl`
- `adjudication.json`

当前 `adjudication.json` 状态是 `pending-human-adjudication`。正式论文结果必须由
非场景作者完成裁决，不能用 fixture 结果代替。

## 5. 仍缺什么

- 其余三个预注册场景的真实模型矩阵；
- token、费用与人工工时账本；
- 非场景作者的独立裁决；
- ReplayNet/真实网络稳健性验证。

## 6. 复现

```bash
make p1-power
make tune-b5-open
make p1-fixture
make analyze-p1
make p1-audit
```
