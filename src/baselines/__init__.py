"""基线策略集合（B0–B5）—— 设计方案 `:848`。

## 这个包的导入图**也是一条安全属性**

这里的东西跑在**策略进程**里（`strategy_child` 按 `pkg.module:run` 加载它们），
所以与 `surface.py` 受同一条约束：**不得**导入 `truth` / `oracle` / `runner` /
`probe_pool` / `iface`，也不得导入 `scoring`。基线策略是「与 Agent 同场竞争的
对手」，它们能看到的必须与 A1/A2 一样多——**多看一眼真值，赢的就不是策略**。

`B5` 的公平性协议（`:643`）把这一条写成了明文：「B5 的作者**不得**接触
`ground_truth()`，只能用 A2 能看到的同样的响应信息。否则是作弊。」

## 各基线的身份（`:848`）

| | 一句话 | 阶段 |
|---|---|---|
| **B0** | 全探一遍（**退化警报**，D10） | Stage 1 |
| B1 | 均匀随机（下界） | Stage 1 |
| B2 | 响应密度贪心（v5 改名：不再吃预言机种子） | Stage 2 |
| B3 | bit-reversal 拟随机（6Seeks Theorem 1） | Stage 2 |
| B4 | 响应率自适应 bandit（**记忆族的无记忆控制组**） | Stage 1 |
| B5 | FRP-aware 手写启发式（**最强的非 LLM 基线**） | Stage 2 |

**B0 的归属是 T13，不是基线交付（T6）**（`:1450`）——它跟配置规格绑在一起，
因为它测的是**配置本身有没有退化**，不是某个策略写得好不好。

## 交付进度（T6 拆成三条，见 `:1443` 那一段）

| | 交付物 | 状态 |
|---|---|---|
| **T6a** | `b1_uniform_random.py`、`b4_response_bandit.py` + 种子通道 | **已交付（2026-10-10）** |
| T6b | `b2.py`、`b3.py`、`b5.py`（Stage 2） | 未开工。**B5 的 (i)/(ii) 归类未定前不得开工** |
| T6c | `m1.py`、`m2.py`（记忆族） | 未开工。**先把 M1/M2 的行为定义写进设计文档**，那是本任务的第一件事 |

**文件名与设计文档的简写不一致，以这里为准。** 设计文档的 T6a 段写的是
`src/baselines/b1.py`、`b4.py`，而 `src/experiment/driver.py` 的
`SMOKE_TABLE_ROWS`（T20 交付，已有测试钉住）写死的是
`b1_uniform_random:run` 与 `b4_response_bandit:run`——与已交付的
`b0_probe_all.py` 同一条命名约定（`b<编号>_<一句话>`）。**简写让位**：
`SMOKE_TABLE_ROWS` 是那个「一键复现」入口实际加载的东西，改它的代价大于
改一句话。

**`B1` 的种子通道就是「T6a 的接口决定」**：`surface.rng()` 拿到的
`random.Random` 由握手送来的 `seed` 派生（协议 v2）。B1 是这条通道的第一个用户，
B4 是第二个。
"""

__all__: list[str] = []
