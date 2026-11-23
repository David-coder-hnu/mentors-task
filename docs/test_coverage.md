# 测试覆盖索引（T8）

这份索引把设计文档点名的路径映射到至少一个可执行测试，不是行覆盖率报告。

## Code paths（30/30）

| 路径 | 测试 |
|---|---|
| `surface.probe` | `test_isolation.py::TestTheRealProcessBoundary`、`TestSurfaceScope` |
| `surface.sample` | `test_isolation.py::TestSurfaceScope` |
| `surface.report` | `test_isolation.py::TestSurfaceScope` |
| `surface.reset` | `test_isolation.py::TestSurfaceScope::test_reset_is_local_and_does_not_touch_the_target` |
| `oracle.ground_truth` | `test_oracle.py::TestOracle` |
| `oracle.reset_target` | `test_oracle.py::TestOracle` |
| JSON config → truth 导出 | `test_ground_truth.py`、`test_stage1_config.py` |
| `decide` | `test_decide.py::TestCacheWiring`、`TestOfflineReplay` |
| schema 拒绝分支 | `test_decide.py::TestParseAction`、`TestMalformedOutputIsCached` |
| 决策缓存读取 | `test_decision_cache.py::TestLookupAndStats`、`TestSaveLoad` |
| 决策缓存写入 | `test_decision_cache.py::TestLookupAndStats`、`TestSaveLoad` |
| 缓存未命中回退 | `test_decide.py::TestOfflineReplay` |
| `score` | `test_score.py` |
| endpoints | `test_score.py::TestTheEndpointResistsGaming`、`test_stats.py` |
| 配对检验 | `test_stats.py::TestWilcoxon` |
| Holm–Bonferroni | `test_stats.py::TestHolm` |
| B0–B5 | `test_b0.py`、`test_b1.py`、`test_b2_b3_b5.py`、`test_b4.py` |
| M1/M2 | `test_m1_m2.py`、`test_memory.py` |
| 预算台账 | `test_isolation.py::TestBudgetIsNotLocal` |
| 丢包/账本对账 | `test_range_logs.py` |
| 在线目标 / RouterShim | `test_objective.py`、`test_router_shim.py` |
| `surface.decide`（runner 侧缓存/模型代理） | `test_stage2.py::TestDecisionBroker`、`test_sandbox.py::TestTheCacheIsAnOpenItem` |
| Stage 2 路由器评分 | `test_stage2.py::test_stage2_score_counts_entities_not_responses` |
| T7 6Sense 去别名化兼容轨道 | `test_6sense_manifest.py`、`test_6sense_compat.py` |
| A1/A2/A3 共用报告与配对分析 | `test_stage2.py`、`test_ablation_analysis.py` |
| A2/A3/B5-open 运行期组合程序 | `test_primitive.py`、`test_stage2.py` |
| P1 四场景与 2×2 分析 | `test_p1_scenarios.py`、`test_p1_analysis.py` |
| B5-open held-out 冻结一致性 | `test_p1_scenarios.py::test_b5_open_code_matches_the_heldout_freeze_record` |
| P1 多来源证据、直接输出审计与 step-only 兼容 | `test_p1_evidence.py` |
| completion token 账本不记录 prompt 或凭据 | `test_responses_completion.py` |

## Runner flows（11/11）

| 流程 | 测试 |
|---|---|
| 单探测成功 | `test_isolation.py::TestSurfaceScope` |
| `send_failed` | `test_response_contract.py`、`test_probe_pool.py`、`test_score.py` |
| `sent_no_reply` | `test_response_contract.py`、`test_probe_pool.py` |
| 并发串包 | `test_probe_pool.py::test_responses_keep_input_order`、`test_foreign_replies_are_counted_not_claimed` |
| 迟到回包 | `test_probe_pool.py` 的 `late` 用例 |
| 队列满丢包 → 整轮无效 | `test_probe_pool.py::TestDrainDuringFill`、`test_range_logs.py` |
| 超预算 → 拒绝 | `test_isolation.py::TestBudgetIsNotLocal` |
| 缓存命中 | `test_decide.py::TestOfflineReplay` |
| 缓存未命中 | `test_decide.py::TestCacheWiring` |
| A1/A2 预算耗尽与最终声明 | `test_stage2.py`、`tests/fixtures/deterministic_agent_completion.py` |
| reset 隔离 | `test_isolation.py::TestSurfaceScope::test_reset_is_local_and_does_not_touch_the_target` |

## CI 四条不可失败项

| 项 | 测试 |
|---|---|
| 能力隔离 | `test_isolation.py::TestImportGraph`、`TestRealProcessBoundary`、`TestTheSandboxIsOnTheRealChild` |
| RouterShim 契约 | `test_router_shim.py` |
| 缓存键跨 `PYTHONHASHSEED` | `test_canonical.py::TestCrossProcessStability`、`test_decision_cache.py::TestCrossProcessKeyStability` |
| 同 config/seed 回包集合稳定 | `test_b1.py`、`test_b4.py`、`test_m1_m2.py` 的稳定性用例 |
