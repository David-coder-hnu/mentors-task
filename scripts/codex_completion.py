"""把当前 Codex CLI 当作 A1/A2 的 completion 命令。

prompt 从 stdin 读，最终消息写到 stdout。这个脚本不读取或打印任何 API key；
认证沿用本机 `codex` CLI 的安全存储。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("stdin 里没有 prompt")

    with tempfile.TemporaryDirectory(prefix="mentors-task-completion-") as tmp:
        output = Path(tmp) / "last-message.txt"
        command = [
            args.codex,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-last-message",
            str(output),
            "-",
        ]
        if args.model:
            command[2:2] = ["--model", args.model]
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-12:]
            raise SystemExit(
                f"codex completion 失败（{result.returncode}）：\n" + "\n".join(tail)
            )
        if not output.exists():
            raise SystemExit("codex 没有写出 final message")
        message = output.read_text(encoding="utf-8").strip()
        if not message:
            raise SystemExit("codex final message 为空")
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
