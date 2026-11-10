# A3 知识消融协议

## 机制

A3 不更换模型。所有任务前缀和探测地址映射为稳定 opaque token：

- 前缀 → `region-<sha256-prefix>`
- 地址 → `node-<sha256-address>`
- 模型只看到 token，不看到 IPv6 文本、前缀长度或编址结构。
- 模型输出的 `region` token 在执行前反解为真实前缀。

这是同构改写，不是弱模型对照。工具数量、预算、epoch、温度、top_p、max_tokens、
schema 与评分器全部保持不变。

## 强制审计

正式运行必须保存：

- 原配置与 A3 配置；
- `same_experiment_fields()` 的比对结果，允许差异只有 `encoding`；
- token 映射的哈希，不公开 salt；
- A3 prompt 中禁止出现 `ipv6`、具体地址、前缀长度或 `prefix` 等领域词汇；
- A3 动作反解日志；
- A1/A2/A3 的相同 `(config, seed)` 配对记录。

## 当前边界

编码机制、prompt 和测试已实现。协议 v4 已接通 A1/A2/A3 的 Stage 2 决策代理；
最终声明改由三行共用的确定性规则产生，因此不再依赖第二次模型调用。30-seed
fixture 预检、A1/A2/A3 同 seed 配对和 900/900 缓存回放均通过。

T16 仍不能标记完成，原因只剩正式模型运行需要轮换后的 provider 凭据、明确
`api_cost_cap` 和冻结模型身份。fixture 结果不得作为知识消融结论。

在看到 A1/A2 的首批决策数据前，不调整 token 形式、预算或模型参数。
