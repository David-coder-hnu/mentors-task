"""Stage 1 真靶场冒烟表的宿主侧编排（T17）。

固定顺序：

1. 重建 Stage 1 target 与日志目录；
2. 在 stage1-harness 中跑五行 × seeds；
3. 正常停止 target，让 `packet_log.txt` flush；
4. 读靶场账本，把逐行结果、账本和退出码写成一份 JSON；
5. 清理容器与网络。

第 3 步不可省。靶场只在自己的正常退出路径 flush 包日志，活着读会得到截断
账本，并把“我们还没 flush”误读成丢包。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = REPO_ROOT / "docker"
RUN_DIR = DOCKER_DIR / "run"
TARGET_LOG_DIR = RUN_DIR / "stage1-target-logs"
ROWS_PATH = RUN_DIR / "stage1-smoke-table.rows.json"
DEFAULT_OUT = RUN_DIR / "stage1-smoke-table.json"
COMPOSE = ["docker", "compose", "--profile", "stage1"]
SETTLE_S = 3.0

sys.path.insert(0, str(REPO_ROOT))
from src.adapter.range_logs import read_ledger  # noqa: E402
from src.experiment.driver import SMOKE_TABLE_ROWS, _parse_seeds  # noqa: E402
from src.experiment.session import load_stage1_context  # noqa: E402


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=DOCKER_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"  $ {' '.join(cmd)}  → {result.returncode}", flush=True)
    if check and result.returncode != 0:
        raise SystemExit(
            f"命令失败（退出码 {result.returncode}）: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _reset_target() -> None:
    _run([*COMPOSE, "rm", "-sf", "stage1-target"], check=False)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if TARGET_LOG_DIR.exists():
        shutil.rmtree(TARGET_LOG_DIR)
    TARGET_LOG_DIR.mkdir(parents=True)
    _ensure_images()
    _run([*COMPOSE, "up", "-d", "stage1-target"])
    time.sleep(SETTLE_S)


def _ensure_images() -> None:
    """缺镜像时才构建；已缓存的镜像不应每次重跑都访问 GitHub。"""

    build_env = dict(os.environ)
    # classic builder avoids an environment-specific Dockerfile-frontend pull;
    # the project Dockerfiles use only classic-compatible features.
    build_env["DOCKER_BUILDKIT"] = "0"
    checks = (
        ("punchingbag:smoke", [*COMPOSE, "build", "target"]),
        ("mentors-task-harness:dev", [*COMPOSE, "--profile", "harness", "build", "harness"]),
    )
    for image, command in checks:
        found = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
        )
        if found.returncode != 0:
            _run(command, env=build_env)


def _run_harness(seeds: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SEEDS"] = seeds
    return _run(
        [*COMPOSE, "run", "--rm", "stage1-harness"],
        check=False,
        env=env,
    )


def _stop_target() -> None:
    _run([*COMPOSE, "stop", "-t", "10", "stage1-target"], check=False)


def _read_rows() -> dict | None:
    if not ROWS_PATH.exists():
        return None
    with ROWS_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _ledger_summary(expected_probes: int) -> dict:
    ledger = read_ledger(TARGET_LOG_DIR)
    out = {
        "has_summary": ledger.has_summary,
        "captured": ledger.captured,
        "expected": expected_probes,
        "enqueued": ledger.enqueued,
        "libnet_errors": ledger.libnet_errors,
        "samples": len(ledger.samples),
    }
    out["valid"] = (
        out["has_summary"]
        and out["captured"] == expected_probes
        and out["enqueued"] <= out["captured"]
        and out["libnet_errors"] == 0
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", default="0-2", help='"0-29" 或 "0,1,2"')
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    seeds = _parse_seeds(args.seeds)
    context = load_stage1_context(REPO_ROOT / "config" / "stage1_config.json")
    expected_probes = context.budget * len(SMOKE_TABLE_ROWS) * len(seeds)

    harness: subprocess.CompletedProcess[str] | None = None
    ledger: dict | None = None
    rows: dict | None = None
    try:
        _reset_target()
        harness = _run_harness(args.seeds)
        # 先停，再读。顺序见模块 docstring。
        _stop_target()
        ledger = _ledger_summary(expected_probes)
        rows = _read_rows()
    finally:
        _run([*COMPOSE, "down", "--remove-orphans"], check=False)

    assert harness is not None
    document = {
        "seed_spec": args.seeds,
        "harness_exit_code": harness.returncode,
        "harness_stdout": harness.stdout,
        "harness_stderr": harness.stderr,
        "rows_file": str(ROWS_PATH),
        "rows": rows,
        "ledger": ledger,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)

    if harness.returncode != 0:
        print(harness.stdout, end="")
        print(harness.stderr, end="", file=sys.stderr)
        print(f"冒烟表失败；证据写到 {args.out}", file=sys.stderr)
        return harness.returncode
    if rows is None or ledger is None or not ledger["valid"]:
        print(
            f"冒烟表缺少结果或靶场账本未通过 captured/libnet 对账；证据写到 {args.out}",
            file=sys.stderr,
        )
        return 2

    print(harness.stdout, end="")
    print(f"冒烟表完成：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
