"""A1/A2 端到端接线测试用的确定性 completion。

**不是模型，也不得产生研究结果。** 它只验证 prompt → runner 缓存/代理 →
策略进程 → 探测账本的跨进程链路。
"""

from __future__ import annotations

import json
import re
import sys


def main() -> int:
    prompt = sys.stdin.read()
    remaining_match = re.search(r"(?:剩余预算|remaining budget)：(\d+)", prompt)
    if remaining_match is None:
        raise SystemExit("prompt 缺少剩余预算")
    remaining = int(remaining_match.group(1))
    epoch_match = re.search(
        r"(?:本 epoch 必须恰好花费：|Each current epoch must spend: )(\d+)",
        prompt,
    )
    epoch_budget = int(epoch_match.group(1)) if epoch_match else remaining
    prefixes = re.findall(r"^- (\S+?/64)：", prompt, re.MULTILINE)
    if remaining < 0:
        raise SystemExit("预算不合法")

    if "balanced_probe" in prompt:
        action = {
            "action": "construct",
            "name": "balanced_probe_smoke",
            "blueprint": "balanced_probe",
            "count": epoch_budget,
            "reason": "确定性接线测试：均摊剩余预算",
        }
    else:
        if not prefixes:
            raise SystemExit("prompt 里找不到前缀")
        action = {
            "action": "probe",
            "prefix": prefixes[0],
            "count": epoch_budget,
            "reason": "确定性接线测试：一次花完预算",
        }
    print(json.dumps(action, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
