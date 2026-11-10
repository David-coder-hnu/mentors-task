# Aliased 端点判定门槛（T14）

本文件在跑表前固定 `aliased_caught` 的防刷规则。

## 证据门槛

一个 truth 前缀被计入 `aliased_caught`，必须同时满足：

- 该前缀至少被探测 2 次；
- 至少收到 2 次回包；
- 策略最终声明 `aliased=True`；
- 该声明通过最长前缀归属到 truth 前缀。

不足证据的 true 声明计入 `aliased_unsubstantiated_claims`，不计入 caught。

## 精度门槛

$$
\mathrm{precision} =
\frac{\mathrm{aliased\_caught}}
{\mathrm{aliased\_caught} + \mathrm{aliased\_false\_positives}}
$$

一个 run 的 aliased 端点进入胜负判据需要：

- `aliased_unsubstantiated_claims == 0`；
- 没有任何 true 声明，或 `precision >= 0.5`。

不满足时 `aliased_endpoint_admissible=False`。该 run 仍可报告诊断列，但不得用
`aliased_caught` 宣胜。

## 为什么是 0.5

“把每个前缀都声明成 aliased”在 8/32 的调参集上只有 0.25 精度，因此会被门槛
拦住；诚实识别 8 个 aliased、并产生期望约 2 个假阳性的 B4 仍有约 0.8 精度。
0.5 是防明显刷分的上限门槛，不是统计检验。

## 多重比较

该门槛只决定单个 run 的端点是否可用。正式主比较仍需：

- 同 seed 配对；
- Holm–Bonferroni 校正；
- `aliased_waste_ratio` 与 `aliased_caught` 的预注册共主判决规则。

禁止在看到结果后修改证据下限、精度门槛或挑一个端点宣布胜利。
