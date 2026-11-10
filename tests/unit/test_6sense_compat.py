"""T7 Linux/arm64 compatibility track inputs and fail-closed behavior."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPAT = REPO_ROOT / "external" / "6sense" / "compat"


def _module():
    spec = importlib.util.spec_from_file_location(
        "sixsense_compat_runner", COMPAT / "compat_runner.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_covers_both_classes_without_overlap() -> None:
    fixture = json.loads((COMPAT / "fixture.json").read_text(encoding="utf-8"))
    aliases = [
        ipaddress.ip_network(value, strict=False)
        for value in fixture["aliased_prefixes"]
    ]
    seeds = [ipaddress.ip_address(value) for value in fixture["seed_addresses"]]
    expected_aliased = {
        str(seed) for seed in seeds if any(seed in net for net in aliases)
    }
    expected_clean = {
        str(ipaddress.ip_address(value))
        for value in fixture["expected_clean_addresses"]
    }
    assert len(expected_aliased) == 4
    assert len(expected_clean) == 2
    assert expected_aliased.isdisjoint(expected_clean)
    assert expected_aliased | expected_clean == {str(seed) for seed in seeds}


def test_aliasv6_output_requires_explicit_boolean_result(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "aliasv6.jsonl"
    output.write_text(
        "\n".join(
            [
                json.dumps(
                    {"ip": "2001:db8:5::1", "result": {"aliased": True}}
                ),
                json.dumps(
                    {"ip": "2001:db8:5:8::1", "result": {"aliased": False}}
                ),
            ]
        ),
        encoding="utf-8",
    )
    aliased, clean = module._read_alias_output(output)
    assert aliased == {"2001:db8:5::1"}
    assert clean == {"2001:db8:5:8::1"}


def test_dockerfile_pins_both_go_components() -> None:
    text = (COMPAT / "Dockerfile").read_text(encoding="utf-8")
    assert "SCANV6_COMMIT=67f6ceca2318017d0a96361056e527020c15b1b0" in text
    assert "ALIASV6_COMMIT=e357120ba138798867059af992647ddddebda39e" in text
    assert "rev-parse HEAD" in text
