"""ROB-259 plist lint tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLIST_DIR = REPO_ROOT / "ops" / "native" / "plists"

PLISTS = [
    "com.robinco.auto-trader.haproxy.plist",
    "com.robinco.auto-trader.api-blue.plist",
    "com.robinco.auto-trader.api-green.plist",
]


@pytest.mark.parametrize("name", PLISTS)
def test_plist_exists(name: str) -> None:
    assert (PLIST_DIR / name).is_file(), f"missing {name}"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available")
@pytest.mark.parametrize("name", PLISTS)
def test_plist_lints(name: str) -> None:
    proc = subprocess.run(
        ["plutil", "-lint", str(PLIST_DIR / name)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_haproxy_plist_label() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.haproxy.plist").read_text()
    assert "<string>com.robinco.auto-trader.haproxy</string>" in body
    # ProgramArguments must route through the wrapper so the haproxy binary
    # is resolved via `command -v haproxy` at runtime (works on Intel + Apple
    # Silicon Homebrew). The plist must NOT hardcode /opt/homebrew/bin/haproxy.
    assert "scripts/run-haproxy.sh" in body
    assert "/opt/homebrew/bin/haproxy" not in body


def test_mac_worker_and_its_log_rotation_plists_are_gone_and_not_deployed() -> None:
    """NCP at-worker is the sole TaskIQ consumer after the 2026-09-02 move."""
    labels = (
        "com.robinco.auto-trader.worker",
        "com.robinco.auto-trader.worker-log-rotation",
    )
    deploy_script = (REPO_ROOT / "scripts" / "deploy-native.sh").read_text()
    single_active = deploy_script.split("SINGLE_ACTIVE_LABELS=(", 1)[1].split(")", 1)[0]

    for label in labels:
        assert not (PLIST_DIR / f"{label}.plist").exists()
        assert label not in single_active


def test_api_blue_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.api-blue.plist").read_text()
    assert "AUTO_TRADER_COLOR" in body and "<string>blue</string>" in body
    assert "AUTO_TRADER_API_PORT" in body and "<string>8001</string>" in body
    assert "current-blue" in body  # WorkingDirectory


def test_api_green_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.api-green.plist").read_text()
    assert "<string>green</string>" in body
    assert "<string>8002</string>" in body
    assert "current-green" in body


def test_mac_mcp_plists_are_gone_and_not_deployed() -> None:
    assert not list(PLIST_DIR.glob("com.robinco.auto-trader.mcp-*.plist"))

    deploy_script = (REPO_ROOT / "scripts" / "deploy-native.sh").read_text()
    deploy_lib = (
        REPO_ROOT / "ops" / "native" / "scripts" / "native_deploy_lib.sh"
    ).read_text()
    for body in (deploy_script, deploy_lib):
        assert "com.robinco.auto-trader.mcp-" not in body
        assert "mcp-active-color" not in body


def test_mac_scheduler_plist_is_gone_and_not_deployed() -> None:
    """The TaskIQ scheduler runs on NCP only (at-scheduler).

    A scheduler plist here or its label in deploy-native.sh resurrects a second
    scheduler on the Mac on every native deploy and double-fires every cron
    task (2026-09-02, twice).
    """
    assert not (PLIST_DIR / "com.robinco.auto-trader.scheduler.plist").exists()
    deploy_script = (REPO_ROOT / "scripts" / "deploy-native.sh").read_text()
    single_active = deploy_script.split("SINGLE_ACTIVE_LABELS=(", 1)[1].split(")", 1)[0]
    assert "com.robinco.auto-trader.scheduler" not in single_active


def test_mac_websocket_plists_are_gone_and_not_deployed() -> None:
    """WebSocket monitors are NCP units so native deploy cannot resurrect them."""
    labels = (
        "com.robinco.auto-trader.kis-websocket",
        "com.robinco.auto-trader.upbit-websocket",
    )
    deploy_script = (REPO_ROOT / "scripts" / "deploy-native.sh").read_text()
    single_active = deploy_script.split("SINGLE_ACTIVE_LABELS=(", 1)[1].split(")", 1)[0]

    for label in labels:
        assert not (PLIST_DIR / f"{label}.plist").exists()
        assert label not in single_active
