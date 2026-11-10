# 6Sense 独立轨道（T7）

6Sense 不经过本仓库的 `ProbeSurface`。它自带生成器、扫描器和预算记账，因此
必须作为独立轨道运行，只比较“预算 → 发现”的汇总结果。

## 上游锁定

- 仓库：`https://github.com/IPv6-Security/6Sense`
- commit：`ad582e5cf9bf8ca2fd4538710bba6b24328b6fa3`
- 许可证：Apache-2.0
- 外部扫描器：`https://github.com/IPv6-Security/scanv6`
- 外部 dealiaser：`https://github.com/IPv6-Security/offline-dealiaser`

本仓库不 vendoring 上游源码；`scripts/run_6sense.sh` 接收已安装的
`SIXSENSE_HOME`。

## 不可省略的输入

1. RouteViews Prefix-to-AS 映射，并按上游 `rounding.py` 预处理；
2. 已知 aliased 前缀列表；允许空文件，但必须在结果中写明“关闭了离线
   dealiasing”；
3. 完整展开的 IPv6 seed 地址集；
4. LSTM checkpoint 目录及 checkpoint 名。

已知 aliased 列表是 6Sense 的辅助输入，**这是预言机信息**。6Sense 因此只能标为
“预言机辅助上界”，不能进入信息条件对等的主胜负带。

## 环境限制

上游 `6sense.yml` 固定 Python 3.11、TensorFlow 2.12 GPU、CUDA 11.3、cuDNN 8.2，
`6sense_runner.py` 默认写死 `gpus=4`。它没有提供当前 macOS arm64 可运行的环境。

真实运行需要满足：

- Linux x86_64；
- 兼容 CUDA 11.3/cuDNN 8.2 的 NVIDIA GPU 环境；
- Conda/mamba；
- 已编译的 `scanv6` 与 `aliasv6`；
- 上述三份数据。

不满足时 `scripts/run_6sense.sh` 会明确拒绝，不会生成伪结果。

## 本机兼容轨道（T7 Verify）

`scripts/run_6sense_compat.sh` 不运行 LSTM 生成器，而是在 Docker 的 Linux
arm64/amd64 环境中原生编译并运行 6Sense 实际使用的两个 Go 组件：

- `offline-dealiaser` / `aliasv6`；
- `scanv6`。

它用已知 aliased 前缀 fixture 验证：

1. 全部 aliased seed 被 `aliasv6` 判为 aliased；
2. aliased 地址进入 scanner 的数量为 0；
3. `scanv6` 模拟模式只收到并命中 clean 地址。

该轨道在 2026-11-10 已通过。它完成 T7 的“0 次探测投给 aliased 前缀”验收，
但不复现完整 LSTM 生成器，不能替代 GPU/checkpoint 环境下的完整 6Sense 运行。
结果见 `docs/results/t7-6sense-compat-2026-11-10.md`。

## 运行

```bash
SIXSENSE_HOME=/path/to/6Sense \
SIXSENSE_PYTHON=/path/to/env/bin/python \
SIXSENSE_SCANNER=/path/to/scanv6 \
SIXSENSE_DEALIASER=/path/to/aliasv6 \
SIXSENSE_BUDGET=1000000 \
SIXSENSE_PPI=100000 \
SIXSENSE_GRADIENT=0.005 \
scripts/run_6sense.sh
```

输出和配置仍由上游 `6sense_runner.py` 管理；本仓库只负责版本、输入与前置条件
审计。
