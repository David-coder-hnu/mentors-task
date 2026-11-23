# 对等投入协议（T19）

## 目的

这份协议回答一个会直接决定 P1 是否成立的问题：B5 是否拿到了与 A2 可比较的
预算、信息和扩展动作空间的权利。

结论先写死：

- **主比较使用 B5-open**：B5 与 A2 拥有相同的初始工具池，也都有权构造新原语。
- **B5-closed 只作消融**：固定初始工具池，用来拆开“开放动作空间”与“构造机制”
  两个因素。
- A2 在运行中构造；B5 在评测冻结前由人构造。二者的构造尝试次数、输入信息、
  验证器与探测预算必须相同，并逐项留痕。

## 1. 同预算

### 运行时预算

A2 与 B5 每个 `(config, seed)` 会话拿到：

- 相同的 `budget`；
- 相同的 `epoch_size`；
- 相同的探测超时和排空窗口；
- 相同的 `max_in_flight`；
- 各自显式记录的 API 调用成本。B5 的 LLM 调用量为 0，A2 按实际模型调用记账。

核对方式：

1. runner 的 `BudgetLedger.total` 必须相等；
2. 每趟 `charged` 必须等于预算；
3. 实验记录必须带 `api_calls`、`prompt_tokens`、`completion_tokens` 和成本。

### 构造预算

评测冻结前预设正整数 `K`，表示最多允许几次新原语尝试：

- B5 可以为 B5-open 提交至多 `K` 个候选原语；
- A2 在运行中至多可以进行 `K` 次原语构造尝试；
- 失败尝试也计数；
- 两种策略使用同一组 schema 校验、安全检查和目标任务验证器。

“人工写了多久”不伪装成可与 token 数直接换算的量。论文必须同时报告 B5 的开发
工时与 A2 的模型调用成本，不能把其中一项藏起来后声称“效率更高”。

## 2. 同信息

B5 与 A2 只能通过 `ProbeSurface` 获得运行期观测。允许读取的字段只有：

- `Response.status`
- `Response.dst`
- `Response.src`
- `Response.error`
- `Response.icmp_type`
- `Response.icmp_code`
- `Response.hop_limit`
- `Response.rtt_ms`

不得读取：

- `GroundTruth` 或 `Oracle`；
- `config/` 中的响应率；
- 其他策略的决策缓存；
- `seed_map`；
- 评测集的 `_role` 或 aliased 标签。

核对方式：

1. `tests/unit/test_isolation.py` 的导入图与真子进程断言；
2. 服务端→客户端每一帧过 `assert_no_truth_keys()`；
3. 每个 epoch 冻结观测快照并记录哈希；
4. A2 与 B5 的观测字段集合由同一 schema 测试比对。

B5 的构造阶段只能接触公开任务说明、初始工具池和训练/留出集中的合法观测；不得
接触评测集真值。

## 3. 同工具池

评测开始前生成 `tool_pool_manifest`，至少记录：

```json
{
  "initial_primitives": ["probe", "sample", "report"],
  "max_new_primitives": 3,
  "validation_schema": "config/protocols/primitive_schema.json",
  "a2_runtime_construction": true,
  "b5_pre_run_construction": true
}
```

规则：

- B5-open 与 A2 的 `initial_primitives` 完全相同；
- 两边都有新增原语的权利；
- 两边必须使用同一验证器；
- 新增原语进入测量前都要通过相同的安全与有效性检查；
- 新原语不能读取真值、网络或 runner 内部对象；
- 评测一旦开始，B5 的工具集冻结；A2 仍可在运行中构造；
- B5-closed 使用同一初始池，但 `max_new_primitives = 0`，只作消融。

2×2 杀死矩阵中的“B5 做到了”指 **B5-open**。若只跑 B5-closed，“B5 做不到”
不能作为 P1 的证据。

## 4. 同裁决人

每项 A2/B5 构造结果由非场景作者独立裁决。裁决只使用预注册标准：

1. 原语是否可执行；
2. 是否通过 schema 与安全检查；
3. 是否在目标任务上产生可观察差异；
4. 是否遵守预算和信息边界；
5. 是否与冻结的工具池清单一致。

裁决者看不到策略名称，先按运行 ID 评分，再解盲。A2 与 B5 使用同一评分表。

## 5. 当前实现状态（2026-11-10）

协议 v4 已实现 A1/A2 的 runner 侧决策代理、100% 决策缓存回放，以及 A2/A3
运行期 `compose` 的安全探测程序 DSL。B5-open 已用同一 DSL 提供一个冻结的手写
程序，四个 P1 场景也能跑通。**这还不是完整的同工具池比较**：

- B5-open 程序尚未在独立留出拓扑上完成调参与冻结审计；
- A2 仍不能执行任意生成代码，只能组合经过验证的 DSL 原语；
- A1/A2/A3/B5-open 的 30-seed fixture 矩阵已完成，但真实模型表尚未跑完；
- 构造物还没有非场景作者的独立裁决结果。

因此本节描述的目标协议仍然有效，但当前结果不得用来宣称 A2 与 B5-open 已满足
对等投入，也不得用来把 P1 判为成立或失败。

## 最小审计包

每次正式比较至少保存：

- `tool_pool_manifest.json`
- `construction_attempts.jsonl`
- `epoch_observations/<strategy>/<seed>.jsonl`
- `api_usage.jsonl`
- `adjudication.json`
- runner 的逐行 `--out` 结果

缺任一文件，该比较不得进入论文主表。
