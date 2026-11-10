# T7：本机兼容的 6Sense 去别名化轨道

日期：2026-11-10

环境：macOS arm64 + Docker `linux/arm64`

判决：**T7 的去别名化验收通过；完整 LSTM 生成器不在本轨道内**

## 1. 为什么可以兼容

完整 6Sense 的生成器依赖未公开随仓库发布的 LSTM checkpoint、TensorFlow 2.12
GPU、CUDA 11.3/cuDNN 8.2，不能在本机原样运行。但 T7 的 Verify 只要求：

> 用已知 aliased 前缀列表跑通，复现“0 次探测投给 aliased 前缀”。

这个性质由 6Sense 的两个 Go 组件决定：

- `offline-dealiaser` / `aliasv6`：按已知 aliased 前缀做精确查表；
- `scanv6`：只接收 dealias 后留下的地址。

二者都能在 Linux arm64 原生编译，因此不需要 GPU，也不需要 LSTM checkpoint。

## 2. 锁定版本

| 组件 | commit |
|---|---|
| 6Sense | `ad582e5cf9bf8ca2fd4538710bba6b24328b6fa3` |
| scanv6 | `67f6ceca2318017d0a96361056e527020c15b1b0` |
| offline-dealiaser | `e357120ba138798867059af992647ddddebda39e` |

Dockerfile 在构建时 checkout 并回读 assert 两个 Go 组件的 commit。

## 3. 结果

输入为 6 个展开 IPv6 seed：

- 4 个落在已知 aliased `/64`；
- 2 个落在 clean `/64`。

`aliasv6` 实测分类：

- aliased：4 / 4；
- clean：2 / 2。

只把 clean 地址写入 `scanv6` 输入：

- scanner targets：2；
- targets 中落在 aliased 前缀的数量：**0**；
- `scanv6` simulation：2 probes、2 successes、0 dropped；
- simulation hits 中落在 aliased 前缀的数量：**0**。

原始证据：`docker/run/6sense-compat.json`。

## 4. 能证明什么

- 锁定版本的 `aliasv6` 会把 fixture 中的全部已知 aliased 地址判为 aliased；
- 4 个 aliased 地址没有任何一个进入 scanner；
- `scanv6` 的输入与模拟输出都只包含预期 clean 地址。

这满足 T7 预注册的“0 次探测投给 aliased 前缀”验收。

## 5. 不能证明什么

- 没有运行 6Sense 的 LSTM breadth/depth 生成器；
- 没有训练或复现上游 checkpoint；
- 没有真实互联网发现性能；
- 没有证明完整 6Sense 在 Punching Bag 上的表 2 全流程等价。

因此本轨道应标记为 **“6Sense dealiasing compatibility”**。完整生成器仍可由
`scripts/run_6sense.sh` 在具备 GPU/checkpoint/数据的环境中运行，但不再是 T7
去别名化验收的前置条件。

## 6. 复现

```bash
make 6sense-compat
cat docker/run/6sense-compat.json
```

也可以直接运行：

```bash
scripts/run_6sense_compat.sh
```
