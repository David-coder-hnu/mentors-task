"""**实验编排层** —— 把 T1–T5 / T10 / T13 交付的部件装成一次可以跑的实验。

这一层在 `T1–T19` 里没有编号。T1–T5 交付的是**部件**：适配层、真值层、评分器、
决策缓存、执行沙箱、配置。而「把部件装成一次实验」此前**只有一处落点**：
`tests/unit/test_b0.py::TestB0ThroughTheRealRunner._session()` —— 一个测试函数，
一个策略，一个 seed。冒烟表要的是 **5 行 × 30 seeds**（`:699`），于是这层必须
从测试里搬出来。

## 四个模块

| 模块 | 是什么 |
|---|---|
| `executors` | 探测出口的两种形态 + `AccumulatingExecutor`（攒整趟的 `Response`） |
| `real` | **测量**那一支：`RawIcmp6Iface` + `ProbePool`。Linux-only，惰性导入 |
| `session` | **一次会话** = 一个 `(策略, seed)`：九步，退出码非零就炸 |
| `driver` | **一张矩阵** = `策略 × seed`，加一条命令行的入口 |

## 一条命令

```
.venv/Scripts/python.exe -m src.experiment --offline --seeds 0-2
```

零参数即可跑（默认 config、默认 B0）。**`--offline` 是排练**：它不出网卡、
只判 `aliased`，**由它得出的数不进入任何报告**。真靶场那一支要 Linux +
`CAP_NET_RAW`。

## 这一层为什么可以 import 真值

`driver` 与 `session` 都会（间接）把 `src.adapter.ground_truth` 拖进
`sys.modules`。这**不是**泄漏：本层住在 **runner 侧**，`Oracle` / 账本 /
`score()` 都在这一侧。泄漏与否的判据是「**策略进程**能不能拿到真值」，而
策略进程只经 stdio 说话（`spawn_strategy`），并且
`tests/unit/test_isolation.py::TestImportGraph` 把 `src.experiment` 一并列进
了策略侧的禁区——**策略一旦 import 本层就变红**。
"""

from .driver import (
    DEFAULT_CONFIG,
    DEFAULT_ROW,
    SMOKE_TABLE_ROWS,
    ExperimentSpec,
    MatrixResult,
    RowResult,
    main,
    run_matrix,
    run_row,
)
from .executors import AccumulatingExecutor, OfflineExecutor, ProbeExecutor
from .session import (
    Sampler,
    SessionFailed,
    SessionResult,
    load_stage1,
    run_session,
    stride_sampler,
)

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_ROW",
    "SMOKE_TABLE_ROWS",
    "AccumulatingExecutor",
    "ExperimentSpec",
    "MatrixResult",
    "OfflineExecutor",
    "ProbeExecutor",
    "RowResult",
    "Sampler",
    "SessionFailed",
    "SessionResult",
    "load_stage1",
    "main",
    "run_matrix",
    "run_row",
    "run_session",
    "stride_sampler",
]
