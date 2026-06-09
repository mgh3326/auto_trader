"""ROB-469 PR1: MCP server lifecycle observability — /health route + lifespan logging.

These tests run with NO database/redis available, which is itself the proof that
/health is dependency-free (a true event-loop liveness probe).
"""

from __future__ import annotations

import logging

import pytest
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
    assert mcp_route.status_code in (400, 401, 406)  # NOT 200 — auth/headers reject


@pytest.mark.unit
def test_lifespan_logs_startup_and_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    mcp = FastMCP(name="lifecycle-test", lifespan=build_server_lifespan(service="test-mcp"))

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
