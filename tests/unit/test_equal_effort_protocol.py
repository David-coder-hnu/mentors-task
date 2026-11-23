"""T19：对等投入协议必须包含四条可审计约束与同一原语 schema。"""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = REPO_ROOT / "docs" / "protocols" / "equal_effort.md"
SCHEMA = REPO_ROOT / "config" / "protocols" / "primitive_schema.json"
PROGRAM_SCHEMA = REPO_ROOT / "config" / "protocols" / "program_schema.json"


class TestEqualEffortProtocol:
    def test_all_four_constraints_have_headings(self) -> None:
        text = PROTOCOL.read_text(encoding="utf-8")
        for title in ("同预算", "同信息", "同工具池", "同裁决人"):
            assert any(
                line.startswith("## ") and title in line
                for line in text.splitlines()
            )

    def test_b5_open_is_the_primary_comparison(self) -> None:
        text = PROTOCOL.read_text(encoding="utf-8")
        assert "主比较使用 B5-open" in text
        assert "B5-closed 只作消融" in text

    def test_the_protocol_has_an_executable_audit_package(self) -> None:
        text = PROTOCOL.read_text(encoding="utf-8")
        for name in (
            "tool_pool_manifest.json",
            "construction_attempts.jsonl",
            "epoch_observations/",
            "api_usage.jsonl",
            "adjudication.json",
        ):
            assert name in text

    def test_primitive_schema_forbids_truth_and_direct_network_access(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        safety = schema["properties"]["safety"]
        assert safety["properties"]["uses_probe_surface_only"]["const"] is True
        assert safety["properties"]["may_read_ground_truth"]["const"] is False
        assert safety["properties"]["may_open_network"]["const"] is False
        assert safety["properties"]["may_start_process"]["const"] is False

    def test_program_schema_is_bounded_and_target_closed(self) -> None:
        schema = json.loads(PROGRAM_SCHEMA.read_text(encoding="utf-8"))
        steps = schema["properties"]["steps"]
        assert steps["maxItems"] == 4
        assert set(steps["items"]["properties"]["target"]["enum"]) == {
            "balanced",
            "least_probed",
            "unprobed",
            "responsive",
        }
