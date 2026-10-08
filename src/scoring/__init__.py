"""评分层 —— **runner 侧**（T4 / D8）。

这一层的输入是「一次运行 + 真值」，输出是**可数量**。它与 `src/adapter/` 的关系
是单向的：评分读适配层导出的真值，适配层不知道评分层的存在——所以
`tests/unit/test_isolation.py` 那条「策略侧导入图里没有真值」的断言与这里无关，
**本层必须有真值**，否则它算的不是分。

Stage 1 的落点是两个共主端点（`aliased_waste_ratio` / `aliased_caught`），
见 `score.py`。`stats.py`（配对检验，T11）与 `endpoints.py`（端点定义，T14）
是同一目录下的后续步骤——**不是并行项**（`:1255`）。
"""
