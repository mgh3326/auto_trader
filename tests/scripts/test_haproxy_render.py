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


def _render(
    api_color: str, mcp_color: str, out_path: Path
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AUTO_TRADER_API_ACTIVE_COLOR": api_color,
        "AUTO_TRADER_MCP_ACTIVE_COLOR": mcp_color,
    }
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
    assert "server api_blue 127.0.0.1:8001 check\n" in body
    assert "server api_green 127.0.0.1:8002 check backup\n" in body
    # MCP same
    assert "server mcp_blue 127.0.0.1:8766 check\n" in body
    assert "server mcp_green 127.0.0.1:8767 check backup\n" in body


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


def test_mcp_backend_health_check_sends_event_stream_accept(tmp_path: Path) -> None:
    """ROB-259 review issue 10: HAProxy's mcp backend health check must send
    the same Accept: text/event-stream header that the native healthcheck and
    cloudflared use, so FastMCP returns the expected 400/401 status. Without
    the header, FastMCP can respond with a different status (e.g. 406) and
    HAProxy would mark the backend DOWN even when the app is healthy.
    """
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "blue", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()

    # The send line must include the Accept header for the MCP backend.
    assert "http-check send" in body, "expected http-check send in bk_mcp"
    assert "Accept text/event-stream" in body, (
        "MCP backend health check is missing the Accept: text/event-stream "
        "header; FastMCP may respond with a non-401 status without it"
    )

    # And expect 400 or 401 (FastMCP unauthenticated reply class)
    assert "http-check expect status 400,401" in body


def test_api_backend_health_check_unchanged(tmp_path: Path) -> None:
    """The api backend continues to use the simple `option httpchk GET /healthz`
    form because /healthz does not depend on any Accept header.
    """
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "blue", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    assert "option httpchk GET /healthz" in body
    assert "http-check expect status 200" in body


def test_no_daemon_directive_in_rendered_config(tmp_path: Path) -> None:
    """ROB-259 review: `daemon` must not be in the global section.

    launchd supervises run-haproxy.sh which exec's `haproxy -W -f ...` in
    the foreground. The `daemon` directive would fork haproxy into the
    background; launchd's KeepAlive=true would then see the parent exit
    and continuously restart, fighting the master-worker model.
    """
    out = tmp_path / "haproxy.cfg"
    proc = _render("blue", "blue", out)
    assert proc.returncode == 0, proc.stderr
    body = out.read_text()
    # The literal `daemon` keyword must not appear as a global directive.
    # Match it as a standalone line to avoid false-positives on words that
    # contain "daemon" (none expected, but defensive).
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "daemon":
            raise AssertionError(
                "rendered haproxy.cfg contains `daemon` directive; remove it "
                "so launchd can foreground-supervise the master process"
            )
    # master-worker is still required for SIGUSR2 seamless reload.
    assert any(line.strip() == "master-worker" for line in body.splitlines())
