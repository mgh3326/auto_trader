"""ROB-259 run-api / run-mcp wrapper smoke tests (no actual server start)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_API = REPO_ROOT / "ops" / "native" / "scripts" / "run-api.sh"
RUN_MCP = REPO_ROOT / "ops" / "native" / "scripts" / "run-mcp.sh"
RUN_MCP_PROFILE = REPO_ROOT / "ops" / "native" / "scripts" / "run-mcp-profile.sh"


def _build_base(tmp_path: Path, color: str) -> Path:
    """Build a fake $AUTO_TRADER_BASE with current-<color>, common.sh, env file, uv stub."""
    base = tmp_path / "services" / "auto_trader"
    (base / f"current-{color}").mkdir(parents=True)
    (base / "shared").mkdir(parents=True)
    (base / "shared" / ".env.prod.native").write_text("# empty env for test\n")
    (base / "scripts").mkdir()
    # Minimal common.sh that mirrors the production behavior the wrapper relies on.
    (base / "scripts" / "common.sh").write_text(
        "#!/bin/zsh\n"
        "set -euo pipefail\n"
        'export AUTO_TRADER_BASE="${AUTO_TRADER_BASE:-$HOME/services/auto_trader}"\n'
        'export AUTO_TRADER_CURRENT="${AUTO_TRADER_CURRENT:-$AUTO_TRADER_BASE/current}"\n'
        'export AUTO_TRADER_ENV_FILE="${AUTO_TRADER_ENV_FILE:-$AUTO_TRADER_BASE/shared/.env.prod.native}"\n'
        'export ENV_FILE="$AUTO_TRADER_ENV_FILE"\n'
        '[[ -d "$AUTO_TRADER_CURRENT" ]] || { echo "AUTO_TRADER_CURRENT missing: $AUTO_TRADER_CURRENT" >&2; exit 70; }\n'
        '[[ -f "$AUTO_TRADER_ENV_FILE" ]] || { echo "AUTO_TRADER_ENV_FILE missing" >&2; exit 78; }\n'
        'cd "$AUTO_TRADER_CURRENT"\n'
        "_export_selected_env_prefixes() {\n"
        '  local prefixes=("$@")\n'
        "  local key value prefix\n"
        '  while IFS="=" read -r key value; do\n'
        '    [[ -z "${key:-}" || "$key" == \\#* ]] && continue\n'
        '    key="${key%%[[:space:]]*}"\n'
        '    for prefix in "${prefixes[@]}"; do\n'
        '      if [[ "$key" == ${prefix}* ]]; then\n'
        '        export "$key=$value"\n'
        "      fi\n"
        "    done\n"
        '  done < "$AUTO_TRADER_ENV_FILE"\n'
        "}\n"
    )
    return base


def _uv_stub_dir(tmp_path: Path) -> Path:
    """uv stub that prints its argv and the value of MCP_PORT and AUTO_TRADER_CURRENT, then exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "argv=$*"\n'
        'echo "MCP_PORT=${MCP_PORT:-unset}"\n'
        'echo "MCP_PROFILE=${MCP_PROFILE:-unset}"\n'
        'echo "MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN:-unset}"\n'
        'echo "AUTO_TRADER_CURRENT=${AUTO_TRADER_CURRENT:-unset}"\n'
        'echo "PWD=$(pwd)"\n'
    )
    stub.chmod(0o755)
    return bin_dir


def _run(
    script: Path, color: str, port_env: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess:
    base = _build_base(tmp_path, color)
    bin_dir = _uv_stub_dir(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_COLOR": color,
        **port_env,
    }
    return subprocess.run(
        ["bash", str(script)], check=False, capture_output=True, text=True, env=env
    )


# ----- run-api ---------------------------------------------------------------


def test_run_api_explicit_port(tmp_path: Path) -> None:
    proc = _run(RUN_API, "blue", {"AUTO_TRADER_API_PORT": "8001"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--port" in proc.stdout
    assert "8001" in proc.stdout


def test_run_api_default_port_blue(tmp_path: Path) -> None:
    proc = _run(RUN_API, "blue", {}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "8001" in proc.stdout


def test_run_api_default_port_green(tmp_path: Path) -> None:
    proc = _run(RUN_API, "green", {}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "8002" in proc.stdout


def test_run_api_invalid_color(tmp_path: Path) -> None:
    proc = _run(RUN_API, "purple", {}, tmp_path)
    assert proc.returncode != 0
    assert "invalid" in proc.stderr.lower() or "purple" in proc.stderr.lower()


def test_run_api_cds_into_color_current(tmp_path: Path) -> None:
    proc = _run(RUN_API, "green", {}, tmp_path)
    assert proc.returncode == 0
    # PWD reflects current-green, not current
    assert "current-green" in proc.stdout


# ----- run-mcp ---------------------------------------------------------------


def test_run_mcp_exports_mcp_port_explicit(tmp_path: Path) -> None:
    proc = _run(RUN_MCP, "blue", {"AUTO_TRADER_MCP_PORT": "8766"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "MCP_PORT=8766" in proc.stdout
    assert "app.mcp_server.main" in proc.stdout


def test_run_mcp_default_port_blue(tmp_path: Path) -> None:
    proc = _run(RUN_MCP, "blue", {}, tmp_path)
    assert proc.returncode == 0
    assert "MCP_PORT=8766" in proc.stdout


def test_run_mcp_default_port_green(tmp_path: Path) -> None:
    proc = _run(RUN_MCP, "green", {}, tmp_path)
    assert proc.returncode == 0
    assert "MCP_PORT=8767" in proc.stdout


def test_run_mcp_invalid_color(tmp_path: Path) -> None:
    proc = _run(RUN_MCP, "purple", {}, tmp_path)
    assert proc.returncode != 0


def test_run_mcp_cds_into_color_current(tmp_path: Path) -> None:
    proc = _run(RUN_MCP, "green", {}, tmp_path)
    assert proc.returncode == 0
    assert "current-green" in proc.stdout


# ----- run-mcp-profile -------------------------------------------------------


def _run_profile(
    script: Path, env_overrides: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess:
    """run-mcp-profile.sh uses a fixed `current` dir, not color-specific."""
    base = _build_base(tmp_path, "blue")
    (base / "current").mkdir(exist_ok=True)
    bin_dir = _uv_stub_dir(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "AUTO_TRADER_BASE": str(base),
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(script)], check=False, capture_output=True, text=True, env=env
    )


def test_run_mcp_profile_exports_fixed_profile_port_and_token(tmp_path: Path) -> None:
    proc = _run_profile(
        RUN_MCP_PROFILE,
        {
            "AUTO_TRADER_MCP_PROFILE": "account_read",
            "AUTO_TRADER_MCP_PORT": "8769",
            "AUTO_TRADER_MCP_AUTH_TOKEN_ENV": "MCP_ACCOUNT_READ_AUTH_TOKEN",
            "MCP_ACCOUNT_READ_AUTH_TOKEN": "account-read-token",
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MCP_PROFILE=account_read" in proc.stdout
    assert "MCP_PORT=8769" in proc.stdout
    assert "MCP_AUTH_TOKEN=account-read-token" in proc.stdout
    assert "current" in proc.stdout


def test_run_mcp_profile_fails_without_dedicated_token(tmp_path: Path) -> None:
    proc = _run_profile(
        RUN_MCP_PROFILE,
        {
            "AUTO_TRADER_MCP_PROFILE": "account_read",
            "AUTO_TRADER_MCP_PORT": "8769",
            "AUTO_TRADER_MCP_AUTH_TOKEN_ENV": "MCP_ACCOUNT_READ_AUTH_TOKEN",
        },
        tmp_path,
    )
    assert proc.returncode == 78
    assert "MCP_ACCOUNT_READ_AUTH_TOKEN is required" in proc.stderr
