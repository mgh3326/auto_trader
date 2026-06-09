"""ROB-259 healthcheck-native --direct mode."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HC = REPO_ROOT / "ops" / "native" / "scripts" / "healthcheck-native.sh"


def _build_curl_stub(tmp_path: Path, port_to_status: dict[int, int]) -> Path:
    """Build a curl stub that maps each port to a curl exit code (0=accepted, !=0=fail).

    For -fsS callers, 200 should yield 0 and anything else 22.
    For non-fsS callers (the MCP probe with -o /dev/null -w '%{http_code}'),
    the stub should print the status code on stdout and exit 0.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    lines = ["#!/usr/bin/env bash", "fsS=0", "capture_status=0", 'url=""']
    lines += [
        'for arg in "$@"; do',
        '  case "$arg" in',
        "    -fsS) fsS=1 ;;",
        "    -sS) capture_status=1 ;;",
        "    -o|-w|-H) ;;",
        "    -*) ;;",
        '    http*|https*) url="$arg" ;;',
        "  esac",
        "done",
        "# Skip the very next-after-flag positional (value for -o or -w or -H) by ignoring; simple stub",
    ]
    # Build the port map as a case statement
    lines += ['case "$url" in']
    for port, status in port_to_status.items():
        # If status is 200 and -fsS used, exit 0; else exit 22.
        # For MCP (-sS, capture_status=1): always print status code, exit 0.
        lines += [
            f"  *127.0.0.1:{port}*)",
            f'    if [[ "$capture_status" == "1" ]]; then echo "{status}"; exit 0;',
            '    elif [[ "$fsS" == "1" ]]; then',
            f'      if [[ "{status}" == "200" ]]; then exit 0; else exit 22; fi',
            "    else exit 0; fi",
            "    ;;",
        ]
    lines += ['  *) echo "no stub for $url" >&2; exit 6 ;;', "esac"]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    return bin_dir


def _common_env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    services_scripts = tmp_path / "services" / "auto_trader" / "scripts"
    services_scripts.mkdir(parents=True, exist_ok=True)
    # minimal common.sh shim so the wrapper can source it
    (services_scripts / "common.sh").write_text(
        '#!/bin/zsh\nset -euo pipefail\nexport AUTO_TRADER_BASE="${AUTO_TRADER_BASE}"\n'
    )
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(tmp_path / "services" / "auto_trader"),
        "AUTO_TRADER_HEALTHCHECK_SKIP_WS": "1",
    }


def test_default_mode_probes_stable_ports(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {8000: 200, 8765: 200})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC)], check=False, capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_direct_blue_probes_8001_and_8766(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {8001: 200, 8766: 200})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC), "--direct", "blue"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_direct_green_probes_8002_and_8767(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {8002: 200, 8767: 200})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC), "--direct", "green"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_direct_rejects_invalid_color(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC), "--direct", "purple"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 64, proc.stderr
    assert "invalid color" in proc.stderr.lower() or "purple" in proc.stderr.lower()


def test_unknown_arg_rejected(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC), "--bogus"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 64, proc.stderr


def test_default_mode_fails_when_api_down(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {8000: 500, 8765: 200})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC)], check=False, capture_output=True, text=True, env=env
    )
    assert proc.returncode != 0


def test_default_mode_fails_when_mcp_returns_unexpected(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {8000: 200, 8765: 500})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC)], check=False, capture_output=True, text=True, env=env
    )
    assert proc.returncode != 0


def test_direct_without_color_exits_64(tmp_path: Path) -> None:
    bin_dir = _build_curl_stub(tmp_path, {})
    env = _common_env(tmp_path, bin_dir)
    proc = subprocess.run(
        ["bash", str(HC), "--direct"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 64, proc.stderr
    assert "blue" in proc.stderr.lower() and "green" in proc.stderr.lower()
