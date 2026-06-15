"""ROB-469 PR1: MCP server lifecycle observability — /health route + lifespan logging.

These tests run with NO database/redis available, which is itself the proof that
/health is dependency-free (a true event-loop liveness probe).
"""

from __future__ import annotations

import logging

import pytest
from unittest.mock import AsyncMock
from fastmcp import FastMCP
from starlette.testclient import TestClient

from app.mcp_server.auth import build_auth_provider
from app.mcp_server.lifecycle import build_server_lifespan, register_health_route


@pytest.mark.unit
def test_health_returns_ok_payload() -> None:
    mcp = FastMCP(name="lifecycle-test")
    register_health_route(mcp, service="test-mcp", version="9.9.9")
    app = mcp.http_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "test-mcp"
    assert body["version"] == "9.9.9"
    assert isinstance(body["uptime_s"], (int, float))


@pytest.mark.unit
def test_health_bypasses_auth_while_mcp_is_gated() -> None:
    # Auth IS enabled, yet /health must still return 200 unauthenticated,
    # while the /mcp protocol route stays gated.
    mcp = FastMCP(name="lifecycle-test", auth=build_auth_provider("super-secret-token"))
    register_health_route(mcp)
    app = mcp.http_app()
    with TestClient(app) as client:
        health = client.get("/health")  # no Authorization header
        mcp_route = client.get("/mcp")  # no Authorization header
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    # Auth-enabled /mcp rejects an unauthenticated request with 401 (RequireAuthMiddleware).
    # Asserting exactly 401 — not a {400,401,406} set — keeps the gating signal real: a
    # 406 (missing MCP Accept headers) is what /mcp returns when auth is DISABLED, so
    # allowing it would let this test false-pass if auth were accidentally broken.
    assert mcp_route.status_code == 401


@pytest.mark.unit
def test_lifespan_logs_startup_and_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    mcp = FastMCP(
        name="lifecycle-test", lifespan=build_server_lifespan(service="test-mcp")
    )

    @mcp.tool
    def echo(x: int) -> int:
        return x

    app = mcp.http_app()
    with caplog.at_level(logging.INFO, logger="app.mcp_server.lifecycle"):
        with TestClient(app):
            pass  # entering runs startup, exiting runs shutdown teardown
    assert "mcp.lifecycle.startup_complete" in caplog.text
    assert "tools=1" in caplog.text  # the single registered tool was counted
    assert "mcp.lifecycle.shutdown" in caplog.text


@pytest.mark.unit
def test_main_module_wires_health_route() -> None:
    # Importing the production module must register /health on the real server
    # instance. _additional_http_routes is where @custom_route appends (fastmcp 3.2.0).
    # (Lifespan wiring is covered by test_lifespan_logs_startup_and_shutdown + the
    # visible FastMCP(lifespan=...) ctor change; FastMCP's default _lifespan is a
    # non-None default_lifespan, so an "is not None" check here would false-pass.)
    import app.mcp_server.main as main_mod

    paths = {getattr(r, "path", None) for r in main_mod.mcp._additional_http_routes}
    assert "/health" in paths


@pytest.mark.unit
def test_lifespan_skips_trade_notifier_when_toss_fill_notify_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.settings, "toss_fill_notify_enabled", False)
    configure = AsyncMock()
    shutdown = AsyncMock()
    monkeypatch.setattr(lifecycle, "configure_trade_notifier_from_settings", configure)
    monkeypatch.setattr(lifecycle, "shutdown_trade_notifier", shutdown)

    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan())
    app = mcp.http_app()
    with TestClient(app):
        pass

    configure.assert_not_called()
    shutdown.assert_not_awaited()


@pytest.mark.unit
def test_lifespan_configures_trade_notifier_when_toss_fill_notify_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mcp_server.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.settings, "toss_fill_notify_enabled", True)
    configure_calls: list[str] = []

    def configure(*, log_context: str) -> bool:
        configure_calls.append(log_context)
        return True

    shutdown = AsyncMock()
    monkeypatch.setattr(lifecycle, "configure_trade_notifier_from_settings", configure)
    monkeypatch.setattr(lifecycle, "shutdown_trade_notifier", shutdown)

    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan())
    app = mcp.http_app()
    with TestClient(app):
        pass

    assert configure_calls == ["MCP trade notifier"]
    shutdown.assert_awaited_once_with(log_context="MCP trade notifier")
