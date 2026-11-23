# ── mentors_task 的本地入口 ──────────────────────────────────────────────────
#
# 门 1 条件 2（`docs/plans/2026-10-03-ceo-plan.md:251`）把「一键复现」拆成两半：
#
#   · **不需要 netns 的那半** → hosted CI（T17 的 `.github/workflows/test.yml`）
#   · **需要 netns 的那半**   → **本文件**（本地 make 目标）
#
# 两半跑的是同一批测试与同一条编排命令，差别只在**跑在哪台机器上**。
# 本文件因此是 T17 的交付物（它的 `Files` 见设计方案 `:1527`），不是另一个任务。
#
# ── 为什么 Makefile 和 `*.sh` 一样必须保持 LF ───────────────────────────────
# 本机 `core.autocrlf=true`，Windows 上 checkout 会把行尾写成 CRLF。配方行以 TAB
# 开头，行尾多出来的那个 `\r` **会被当成命令的一部分**——症状是
# `make: *** [target] Error 127`，看起来像「命令不存在」。`.gitattributes` 里有
# 一条 `Makefile text eol=lf` 钉住它，与 `*.sh` / `Dockerfile*` / `*.yml` 同一条理由。
#
# ── 这个文件是给 Linux 用的，而它**尚未在开发机上执行过** ───────────────────
# 需要真靶场的目标在 Windows 宿主上做不了（建不了 ICMPv6 原始套接字，见
# `tests/integration/conftest.py` 的 skipif）。开发机装了 Docker 但**没有 make**，
# 所以本文件是照既有命令拼出来的，**没有新增任何语义**——每一条配方都对应一处
# 已经在仓库里跑过的调用。这层「未实测」是记着的，不是默认的。

ifeq ($(OS),Windows_NT)
PY ?= .venv/Scripts/python.exe
else
PY ?= .venv/bin/python
endif

#: 冒烟表的 seed 范围。默认 3 个够验证链路；正式那一轮是 0-29（30 个配对单位）。
SEEDS ?= 0-2

.DEFAULT_GOAL := help
.PHONY: help test test-unit smoke-offline heldout harness range-smoke range-down sweep smoke-table analyze-stage1 stage2-baselines analyze-stage2 ablation-fixture analyze-ablation p1-fixture analyze-p1 p1-power p1-audit 6sense-compat

# ── 帮助 ─────────────────────────────────────────────────────────────────────

help:
	@echo "mentors_task —— 本地入口（门 1 条件 2 的 netns 半边）"
	@echo ""
	@echo "  make test            单测。hosted CI 跑的就是这一条"
	@echo "  make test-unit       只跑 tests/unit"
	@echo "  make smoke-offline   编排层排练：策略 × seed 的矩阵，不出网卡"
	@echo "  make heldout         重建 Stage 1 留出集（固定 seed）"
	@echo "  make harness         在 harness 容器里跑 tests/integration（需要 Docker）"
	@echo "  make range-smoke     T1 的靶场冒烟：正例 5/5 + 负对照 0/5"
	@echo "  make range-down      停掉靶场容器"
	@echo "  make sweep           吞吐标定（产出 docs/perf_baseline.md 的素材）"
	@echo "  make smoke-table     Stage 1 五行 × seeds 的真靶场冒烟表"
	@echo "  make analyze-stage1  对最近一次 Stage 1 结果做配对检验与分布"
	@echo "  make stage2-baselines  Stage 2 六行 × seeds（RouterShim，非外网）"
	@echo "  make analyze-stage2   对最近一次 Stage 2 基线做分布与配对比较"
	@echo "  make ablation-fixture A1/A2/A3 × 30 seeds 的确定性工程预检（不是模型结果）"
	@echo "  make analyze-ablation 对最近一次 A1/A2/A3 矩阵做同 seed 配对分析"
	@echo "  make p1-fixture       在 3 个预注册场景运行 A1/A2/A3/B5-open/B5-closed 工程预检"
	@echo "  make analyze-p1       填充 P1 的 2×2 工程矩阵"
	@echo "  make p1-power         检查 P1 的预注册功效敏感度"
	@echo "  make p1-audit         生成 P1 构造、API 与 epoch 审计包"
	@echo "  make 6sense-compat    T7：Linux/arm64 容器中的 aliasv6 + scanv6 去别名化验收"
	@echo ""
	@echo "  PY=$(PY)"
	@echo "  SEEDS=$(SEEDS)   （smoke-offline / smoke-table 都可用 SEEDS=0-29）"

# ── 宿主侧就能跑的（与 CI 同一条） ───────────────────────────────────────────

test:
	$(PY) -m pytest tests

test-unit:
	$(PY) -m pytest tests/unit

#: 排练靶场（不出网卡）。**由它得出的数不进入报告**——回包判据是按 `aliased`
#: 前缀合成出来的，不是真靶场给的。它的用途是验证编排链路本身。
smoke-offline:
	$(PY) -m src.experiment --offline --seeds $(SEEDS) \
		--strategy src.baselines.b0_probe_all:run \
		--strategy src.baselines.b1_uniform_random:run \
		--strategy src.baselines.b4_response_bandit:run \
		--strategy src.baselines.m1_memory_cold:run \
		--strategy src.baselines.m2_memory_warm:run

#: 重建 T18 的留出集。固定 seed，产物可直接 diff。
heldout:
	$(PY) -m scripts.make_heldout

#: 吞吐标定。脚本自己 `cwd=docker/`，所以在仓库根调用即可。
sweep:
	$(PY) scripts/perf_sweep.py

# ── 需要 Docker / netns 的 ───────────────────────────────────────────────────

#: `tests/integration/` 的实弹测试。`conftest.py` 里的 skipif 在宿主上会 skip，
#: 所以这一条**必须**走容器，否则跑出来是「全 skip 也算过」。
harness:
	cd docker && docker compose --profile harness run --rm harness

#: T1 的验收装置：容器内系统 `ping` 打靶场。正例 5/5、负对照 0/5，退出码 0 即通过。
#: 失败时也要 `down`，否则下次 `up` 会撞上残留的网络。
range-smoke:
	@cd docker && { docker compose up --build --abort-on-container-exit --exit-code-from prober; \
	  code=$$?; docker compose down; exit $$code; }

range-down:
	cd docker && docker compose down

#: Stage 1 的真靶场：五行 × seeds。宿主脚本负责起靶场、跑 harness、**先停靶场
#: 再读账本**以及清理。默认 0-2 是排练规模；正式入口传 `SEEDS=0-29`。
smoke-table:
	$(PY) scripts/run_smoke_table.py --seeds $(SEEDS)

analyze-stage1:
	$(PY) scripts/analyze_smoke_table.py docker/run/stage1-smoke-table.json

stage2-baselines:
	$(PY) -m src.experiment --stage 2 --seeds $(SEEDS) \
		--strategy src.baselines.b0_probe_all:run \
		--strategy src.baselines.b1_uniform_random:run \
		--strategy src.baselines.b2_response_density:run \
		--strategy src.baselines.b3_bit_reversal:run \
		--strategy src.baselines.b4_response_bandit:run \
		--strategy src.baselines.b5_frp_aware:run \
		--out docker/run/stage2-baselines.json

analyze-stage2:
	$(PY) scripts/analyze_stage2.py docker/run/stage2-baselines.json

ablation-fixture:
	$(PY) -m src.experiment --stage 2 --seeds $(SEEDS) \
		--strategy src.agent.strategies:run_a1 \
		--strategy src.agent.strategies:run_a2 \
		--strategy src.agent.strategies:run_a3 \
		--completion-command '$(PY) tests/fixtures/deterministic_agent_completion.py' \
		--decision-cache docker/run/stage2-a123-fixture-cache.jsonl \
		--out docker/run/stage2-a123-fixture.json

analyze-ablation:
	$(PY) scripts/analyze_ablation.py docker/run/stage2-a123-fixture.json

p1-fixture:
	$(PY) scripts/run_p1_fixture.py

analyze-p1:
	$(PY) scripts/analyze_p1.py docker/run/p1-fixture/manifest-results.json

p1-power:
	$(PY) scripts/check_p1_power.py

p1-audit:
	$(PY) scripts/make_p1_audit.py docker/run/p1-fixture/manifest-results.json

6sense-compat:
	bash scripts/run_6sense_compat.sh
