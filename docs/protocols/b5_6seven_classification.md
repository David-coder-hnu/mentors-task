# B5 / 6SEVEN 归类

**结论：6SEVEN 的 online alias detection 属于 (ii) 固定形状的检测组件，
不是跨前缀预算自适应策略。**

## 源码依据

- 上游仓库：`https://github.com/0xchase/6SEVEN`
- 阅读 commit：`f22f6670790912e8b5eb8b01c7280988b6a2f4b3`
  （`publish latest source`，2026-09-08）
- 关键文件：
  - `scan/src/dealias/mod.rs`
  - `scan/src/dealias/detector.rs`
  - `cli/src/commands/dealias.rs`

`OnlineConfig` 默认检查六个前缀长度：

```text
48, 64, 96, 112, 116, 120
```

每个唯一前缀为每种长度生成三个不同目标；命中两个 Echo Reply 即判定 aliased。
因此完整检测代价是：

```text
6 lengths × 3 probes = 18 probes / unique prefix
```

`Detector::consider()` 只决定当前地址是否需要排队做 alias 测试。它不根据历史
回报计算区域价值，也不在不同 /48 之间重新分配剩余探测预算。运行期扫描可以把
它作为 dealiasing 层调用，但该层本身是固定检测组件。

## 对 B5 的含义

B5 应实现为：

1. 6SEVEN 固定的 18 探 alias 检测；
2. 显式组合一个后续地址发现分配规则；
3. 两部分从同一 `budget` 扣费；
4. 检测发现 aliased 后停止向该前缀继续投入；
5. 论文中明确 B5 不是 6SEVEN 的完整扫描器复现，而是“6SEVEN dealiasing +
   本文选择的发现分配器”。

不得把 B5 写成“6SEVEN 的插件层完整复现”。上游插件框架负责托管 TGA、共享
dealiasing 与汇总；本仓库只借用其已发表 alias 检测规则。

## 预算约束

Stage 2 的 1000 探不保证能覆盖任意数量的前缀：

```text
floor(1000 / 18) = 55 unique prefixes
```

这只是上界；B5 还要从同一预算中支出发现探测。若评测 config 的前缀数超过该
可覆盖范围，B5 必须按预注册顺序处理前缀，并在结果中报告未检测前缀数。
Stage 1 的 300 探最多支撑 16 个完整 alias 测试，因此 B5 继续留在 Stage 2。
