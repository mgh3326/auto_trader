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
    "com.robinco.auto-trader.mcp-blue.plist",
    "com.robinco.auto-trader.mcp-green.plist",
]


@pytest.mark.parametrize("name", PLISTS)
def test_plist_exists(name: str) -> None:
    assert (PLIST_DIR / name).is_file(), f"missing {name}"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available")
@pytest.mark.parametrize("name", PLISTS)
def test_plist_lints(name: str) -> None:
    proc = subprocess.run(["plutil", "-lint", str(PLIST_DIR / name)], check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_haproxy_plist_label() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.haproxy.plist").read_text()
    assert "<string>com.robinco.auto-trader.haproxy</string>" in body
    assert "haproxy" in body  # ProgramArguments references haproxy binary
    assert "shared/haproxy/haproxy.cfg" in body


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


def test_mcp_blue_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.mcp-blue.plist").read_text()
    assert "<string>blue</string>" in body
    assert "<string>8766</string>" in body
    assert "current-blue" in body


def test_mcp_green_plist_port() -> None:
    body = (PLIST_DIR / "com.robinco.auto-trader.mcp-green.plist").read_text()
    assert "<string>green</string>" in body
    assert "<string>8767</string>" in body
    assert "current-green" in body
