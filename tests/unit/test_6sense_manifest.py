"""T7 前置审计：锁定来源、许可证、辅助数据与失败即拒绝的 wrapper。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "external" / "6sense" / "manifest.json"
WRAPPER = REPO_ROOT / "scripts" / "run_6sense.sh"


class TestSixSenseManifest:
    def test_upstream_and_license_are_pinned(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["repository"] == "https://github.com/IPv6-Security/6Sense"
        assert manifest["commit"] == "ad582e5cf9bf8ca2fd4538710bba6b24328b6fa3"
        assert manifest["license"] == "Apache-2.0"
        assert manifest["role"] == "oracle-assisted external upper bound"

    def test_all_external_inputs_are_named(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert set(manifest["required_inputs"]) == {
            "prefix_to_as",
            "aliased_prefixes",
            "expanded_ipv6_seed_addresses",
            "lstm_checkpoint",
        }
        assert manifest["scanner_commit"] == "67f6ceca2318017d0a96361056e527020c15b1b0"
        assert (
            manifest["offline_dealiaser_commit"]
            == "e357120ba138798867059af992647ddddebda39e"
        )
        assert manifest["compat_runtime"]["gpu_required"] is False

    def test_wrapper_refuses_missing_prerequisites(self) -> None:
        env = dict(os.environ)
        for name in (
            "SIXSENSE_HOME",
            "SIXSENSE_PYTHON",
            "SIXSENSE_SCANNER",
            "SIXSENSE_DEALIASER",
            "SIXSENSE_BUDGET",
            "SIXSENSE_PPI",
            "SIXSENSE_GRADIENT",
        ):
            env.pop(name, None)
        result = subprocess.run(
            ["bash", str(WRAPPER)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "前置条件缺失" in result.stderr
