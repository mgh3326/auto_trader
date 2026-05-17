"""ROB-259 haproxy_switch tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SWITCH = REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_switch.sh"
TEMPLATE = REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl"


def _run(env_extra: dict[str, str], base: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AUTO_TRADER_BASE": str(base),
        "AUTO_TRADER_HAPROXY_TEMPLATE": str(TEMPLATE),
        # Default: skip the actual launchctl reload so the test doesn't need root/launchd.
        "AUTO_TRADER_HAPROXY_RELOAD": "skip",
        **env_extra,
    }
    return subprocess.run(["bash", str(SWITCH)], check=False, capture_output=True, text=True, env=env)


def _setup_base(tmp_path: Path) -> Path:
    (tmp_path / "shared" / "haproxy").mkdir(parents=True)
    (tmp_path / "shared").joinpath("api-active-color").write_text("blue\n")
    (tmp_path / "shared").joinpath("mcp-active-color").write_text("blue\n")
    return tmp_path


def test_switch_writes_live_config(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    proc = _run({}, base)
    assert proc.returncode == 0, proc.stderr
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    body = live.read_text()
    assert "bind 127.0.0.1:8000" in body
    assert "server api_blue 127.0.0.1:8001 check\n" in body


def test_switch_atomically_replaces_existing(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    live.write_text("stale\n")
    stat_before = live.stat()
    proc = _run({}, base)
    assert proc.returncode == 0, proc.stderr
    stat_after = live.stat()
    # mv-based replace must change the inode
    assert stat_before.st_ino != stat_after.st_ino
    assert "bind 127.0.0.1:8000" in live.read_text()


def test_switch_rolls_back_on_validation_failure(tmp_path: Path) -> None:
    base = _setup_base(tmp_path)
    live = base / "shared" / "haproxy" / "haproxy.cfg"
    live.write_text("KEEP-ME\n")
    # Force the renderer to error out by passing an invalid color.
    (base / "shared" / "api-active-color").write_text("purple\n")
    proc = _run({}, base)
    assert proc.returncode != 0
    assert live.read_text() == "KEEP-ME\n"
