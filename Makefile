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
.PHONY: help test test-unit smoke-offline harness range-smoke range-down sweep smoke-table

# ── 帮助 ─────────────────────────────────────────────────────────────────────

help:
	@echo "mentors_task —— 本地入口（门 1 条件 2 的 netns 半边）"
	@echo ""
	@echo "  make test            单测。hosted CI 跑的就是这一条"
	@echo "  make test-unit       只跑 tests/unit"
	@echo "  make smoke-offline   编排层排练：策略 × seed 的矩阵，不出网卡"
	@echo "  make harness         在 harness 容器里跑 tests/integration（需要 Docker）"
	@echo "  make range-smoke     T1 的靶场冒烟：正例 5/5 + 负对照 0/5"
	@echo "  make range-down      停掉靶场容器"
	@echo "  make sweep           吞吐标定（产出 docs/perf_baseline.md 的素材）"
	@echo "  make smoke-table     [不能跑] 冒烟表的真靶场实跑 —— 会拒绝并列出缺什么"
	@echo ""
	@echo "  PY=$(PY)"
	@echo "  SEEDS=$(SEEDS)   （make smoke-offline SEEDS=0-29）"

# ── 宿主侧就能跑的（与 CI 同一条） ───────────────────────────────────────────

test:
	$(PY) -m pytest tests

test-unit:
	$(PY) -m pytest tests/unit

#: 排练靶场（不出网卡）。**由它得出的数不进入报告**——回包判据是按 `aliased`
#: 前缀合成出来的，不是真靶场给的。它的用途是验证编排链路本身。
smoke-offline:
	$(PY) -m src.experiment --offline --seeds $(SEEDS)

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

#: ⚠️ **门 1 条件 1 / 2 的那条腿，今天不能跑。** 它必须大声拒绝，不能静默。
#:
#: 它应该在真靶场上跑冒烟表（B0/B1/B4/M1/M2 × 30 seeds），并把逐行记录落到
#: `--out`。缺三样东西，缺哪一样都会让结果**看起来正常**，所以列出来：
#:
#:   1. **靶场没有按 Stage 1 的 config 起。** `docker/compose.yml` 的 `target`
#:      挂的是 `docker/smoke/punchingbag-smoke.json`——那是 T1 的脚手架，两个
#:      前缀（`dead` / `0bad`），不是 Stage 1 的 32 个 `/64`。要让冒烟表量的是
#:      Stage 1，得从 `config/stage1_config.json` 生成一份 Punching Bag 的
#:      `--json-config`，并给 `2001:db8:5::/48` 配路由。这件事 T13 已具名记在
#:      `:1780`（「要么改 compose.yml，要么新起一个 profile」），**当时没有归属**。
#:   2. **缺一个宿主侧编排脚本。** 形状与 `scripts/perf_sweep.py` 完全一样：
#:      起靶场 → 容器内跑编排层 → **停靶场** → 读账本。第 3 步不可省——靶场的
#:      `packet_log.txt` 只在正常退出时 flush，活着读会读到被截断的文件，于是
#:      「我们发了多少」对不上，而那个假报**指向保守结论**，不会有人质疑。
#:   3. **冒烟表 5 行里 4 行缺一条种子通道**（B1/B4/M1/M2）。契约是
#:      `run(surface) -> None`，没有 seed 参数；那条通道走握手还是走环境变量要动
#:      `T2` 的协议面，**是 T6a 的接口决定**（见 T20 完成块第四、六节）。
#:
#: 这三样都在 T17 里（第 1、2 项是它的 netns 半边，第 3 项在 T6a）。
#: **在它们落地之前，这条目标宁可炸，也不返回一张看起来完整的表。**
smoke-table:
	@echo "" >&2
	@echo "make smoke-table: 不能跑 —— 门 1 条件 2 的 netns 半边还没接线。" >&2
	@echo "" >&2
	@echo "  1) 靶场没按 Stage 1 的 config 起（挂的是 T1 的 smoke 脚手架，" >&2
	@echo "     docker/smoke/punchingbag-smoke.json；见设计方案 :1780）" >&2
	@echo "  2) 缺宿主侧编排脚本（起靶场 → 容器内跑 → 停靶场 → 读账本，" >&2
	@echo "     与 scripts/perf_sweep.py 同一形状）" >&2
	@echo "  3) B1/B4/M1/M2 缺种子通道（归 T6a；今天只有 B0 能真跑）" >&2
	@echo "" >&2
	@echo "中间形态：make smoke-offline（排练靶场，出不了网卡，数不进报告）" >&2
	@echo "" >&2
	@exit 1
