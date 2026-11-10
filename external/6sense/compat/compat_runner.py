"""Run 6Sense's exact dealiasing gate without its LSTM dependency."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ALIASV6 = "/usr/local/bin/aliasv6"
SCANV6 = "/usr/local/bin/scanv6"


class CompatError(RuntimeError):
    pass


def _network(value: str) -> ipaddress.IPv6Network:
    network = ipaddress.ip_network(value, strict=False)
    if not isinstance(network, ipaddress.IPv6Network):
        raise CompatError(f"{value} 不是 IPv6 前缀")
    return network


def _address(value: str) -> ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if not isinstance(address, ipaddress.IPv6Address):
        raise CompatError(f"{value} 不是 IPv6 地址")
    return address


def _run(command: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-20:]
        raise CompatError(
            f"命令失败（{result.returncode}）：{' '.join(command)}\n"
            + "\n".join(tail)
        )
    return result


def _read_alias_output(path: Path) -> tuple[set[str], set[str]]:
    aliased: set[str] = set()
    clean: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CompatError(f"{path}:{lineno} 不是 JSON") from exc
        address = str(_address(record["ip"]))
        result = record.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("aliased"), bool):
            raise CompatError(f"{path}:{lineno} 没有布尔 result.aliased")
        (aliased if result["aliased"] else clean).add(address)
    return aliased, clean


def _run_dealiaser(
    work: Path,
    prefixes: list[ipaddress.IPv6Network],
    addresses: list[ipaddress.IPv6Address],
) -> tuple[set[str], set[str]]:
    prefix_file = work / "aliased-prefixes.txt"
    commands = work / "lookup.jsonl"
    output = work / "aliasv6.jsonl"
    metadata = work / "aliasv6.meta"
    log = work / "aliasv6.log"
    prefix_file.write_text("".join(f"{network}\n" for network in prefixes), encoding="utf-8")
    commands.write_text(
        "".join(
            json.dumps({"type": "lookup", "data": str(address)}) + "\n"
            for address in addresses
        ),
        encoding="utf-8",
    )
    result = _run(
        [
            ALIASV6,
            "--flush",
            "--expanded",
            "-c",
            str(prefix_file),
            "-f",
            str(commands),
            "-o",
            str(output),
            "-m",
            str(metadata),
            "-l",
            str(log),
        ]
    )
    if "panic" in result.stderr.lower() or "fatal" in result.stderr.lower():
        raise CompatError(f"aliasv6 报告失败：\n{result.stderr[-2000:]}")
    return _read_alias_output(output)


def _run_scanner(work: Path, clean: list[str]) -> tuple[list[str], dict[str, Any]]:
    targets = work / "scanner-targets.txt"
    hits = work / "scanv6.hits"
    metadata = work / "scanv6.meta"
    log = work / "scanv6.log"
    targets.write_text("".join(f"{address}\n" for address in clean), encoding="utf-8")
    result = _run(
        [
            SCANV6,
            "-f",
            str(targets),
            "-o",
            str(hits),
            "-m",
            str(metadata),
            "-l",
            str(log),
            "--blocklist-file",
            "/dev/null",
            "--cooldown-time",
            "0",
            "--senders",
            "1",
            "--receivers",
            "1",
            "--rate",
            "1000",
            "--simulation",
            "--simulation-hitrate",
            "100",
            "--expanded",
            "--interface",
            os.environ.get("SIXSENSE_IFACE", "eth0"),
            "--source-ip",
            "2001:db8:ffff:ffff::1",
            "--gateway-mac",
            "02:00:00:00:00:01",
            "icmp6_echoscan",
        ],
        timeout=60.0,
    )
    if "fatal" in result.stderr.lower():
        raise CompatError(f"scanv6 报告失败：\n{result.stderr[-2000:]}")
    observed = [
        str(_address(line))
        for line in hits.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scan_meta = [
        json.loads(line)
        for line in metadata.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = next(
        (
            item
            for item in reversed(scan_meta)
            if isinstance(item, dict) and item.get("mode", "").startswith("simulation")
        ),
        {},
    )
    return observed, summary


def execute(fixture_path: Path, output_path: Path, *, scanner: bool) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    prefixes = [_network(value) for value in fixture["aliased_prefixes"]]
    addresses = [_address(value) for value in fixture["seed_addresses"]]
    expected_clean = {str(_address(value)) for value in fixture["expected_clean_addresses"]}
    if len({str(address) for address in addresses}) != len(addresses):
        raise CompatError("seed_addresses 有重复")

    expected_aliased = {
        str(address)
        for address in addresses
        if any(address in network for network in prefixes)
    }
    if expected_clean & expected_aliased:
        raise CompatError("expected_clean_addresses 与 aliased 前缀相交")
    if expected_clean | expected_aliased != {str(address) for address in addresses}:
        raise CompatError("每个 seed 必须被 fixture 明确归为 aliased 或 clean")

    with tempfile.TemporaryDirectory(prefix="6sense-compat-") as raw_work:
        work = Path(raw_work)
        classified_aliased, classified_clean = _run_dealiaser(work, prefixes, addresses)
        scanner_targets = sorted(classified_clean)
        aliased_in_scanner_input = sorted(
            address
            for address in scanner_targets
            if any(ipaddress.ip_address(address) in network for network in prefixes)
        )
        classification_ok = (
            classified_aliased == expected_aliased
            and classified_clean == expected_clean
            and not aliased_in_scanner_input
        )
        observed_hits: list[str] = []
        scan_summary: dict[str, Any] = {}
        if scanner:
            observed_hits, scan_summary = _run_scanner(work, scanner_targets)

    hit_aliases = [
        address
        for address in observed_hits
        if any(ipaddress.ip_address(address) in network for network in prefixes)
    ]
    scanner_ok = (
        not scanner
        or (
            set(observed_hits) == set(scanner_targets)
            and not hit_aliases
        )
    )
    passed = classification_ok and scanner_ok
    result = {
        "status": "pass" if passed else "fail",
        "scope": fixture.get("scope", "6Sense dealiasing compatibility"),
        "validity": {
            "proves": [
                "pinned aliasv6 classifies known aliased prefixes as aliased",
                "scanv6 receives zero addresses from aliased prefixes",
                "scanv6 simulation reaches only the expected clean addresses",
            ],
            "does_not_prove": [
                "full 6Sense LSTM generation behavior",
                "real-internet discovery performance",
                "Punching Bag table-2 parity beyond the dealiasing gate",
            ],
        },
        "inputs": {
            "seed_addresses": len(addresses),
            "aliased_prefixes": len(prefixes),
            "expected_aliased_addresses": len(expected_aliased),
            "expected_clean_addresses": len(expected_clean),
        },
        "aliasv6": {
            "classified_aliased": sorted(classified_aliased),
            "classified_clean": sorted(classified_clean),
            "classification_ok": classification_ok,
        },
        "scanner": {
            "enabled": scanner,
            "mode": "simulation" if scanner else "skipped",
            "targets": scanner_targets,
            "observed_hits": sorted(observed_hits),
            "aliased_targets": aliased_in_scanner_input,
            "aliased_hits": hit_aliases,
            "summary": scan_summary,
            "ok": scanner_ok,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not passed:
        raise CompatError(f"兼容轨道验收失败，证据见 {output_path}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-scanner", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.input, args.out, scanner=not args.skip_scanner)
    except (CompatError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "status": result["status"],
        "aliased_targets": len(result["scanner"]["aliased_targets"]),
        "scanner_targets": len(result["scanner"]["targets"]),
        "observed_hits": len(result["scanner"]["observed_hits"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
