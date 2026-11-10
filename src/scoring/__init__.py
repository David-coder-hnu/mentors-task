"""评分层 —— **runner 侧**（T4 / D8）。

这一层的输入是「一次运行 + 真值」，输出是**可数量**。它与 `src/adapter/` 的关系
是单向的：评分读适配层导出的真值，适配层不知道评分层的存在——所以
`tests/unit/test_isolation.py` 那条「策略侧导入图里没有真值」的断言与这里无关，
**本层必须有真值**，否则它算的不是分。

Stage 1 的落点是两个共主端点（`aliased_waste_ratio` / `aliased_caught`），
见 `score.py`。`endpoints.py`（T14）已交付证据与精度门槛，`stats.py` 与
`report.py`（T11）已交付配对 Wilcoxon、Holm 校正和逐 seed 分布报告。
"""

from .stage2 import Stage2Score, score_stage2

__all__ = ["Stage2Score", "score_stage2"]
