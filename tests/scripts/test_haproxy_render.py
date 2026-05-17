"""ROB-259 HAProxy config render tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER = REPO_ROOT / "ops" / "native" / "scripts" / "haproxy_render.sh"
TEMPLATE = REPO_ROOT / "ops" / "native" / "haproxy" / "haproxy.cfg.tmpl"


def _render(api_color: str, mcp_color: str, out_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "AUTO_TRADER_API_ACTIVE_COLOR": api_color, "AUTO_TRADER_MCP_ACTIVE_COLOR": mcp_color}
    return subprocess.run(
        ["bash", str(RENDER), str(TEMPLATE), str(out_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_render_blue_blue(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "blue", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    # Stable listeners always present
    assert "bind 127.0.0.1:8000" in body
    assert "bind 127.0.0.1:8765" in body
    # API blue active backend is :8001, green is backup
    assert "server api_blue 127.0.0.1:8001 check" in body
    assert "server api_green 127.0.0.1:8002 check backup" in body
    # MCP same
    assert "server mcp_blue 127.0.0.1:8766 check" in body
    assert "server mcp_green 127.0.0.1:8767 check backup" in body


def test_render_blue_green_mixed(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "green", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    assert "server api_blue 127.0.0.1:8001 check\n" in body
    assert "server api_green 127.0.0.1:8002 check backup\n" in body
    assert "server mcp_green 127.0.0.1:8767 check\n" in body
    assert "server mcp_blue 127.0.0.1:8766 check backup\n" in body


def test_render_rejects_invalid_color(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    proc = _render("purple", "blue", out)
    assert proc.returncode != 0
    assert "invalid color" in proc.stderr.lower()


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="haproxy not installed")
def test_rendered_config_validates(tmp_path: Path) -> None:
    out = tmp_path / "haproxy.cfg"
    _render("blue", "blue", out)
    proc = subprocess.run(
        ["haproxy", "-c", "-f", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
