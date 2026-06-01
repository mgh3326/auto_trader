"""ROB-405 Slice D — get_mock_loop_retrospective MCP tool."""

from __future__ import annotations

import pytest

from tests._mcp_tooling_support import build_tools


def test_tool_registered():
    tools = build_tools()
    assert "get_mock_loop_retrospective" in tools


def test_tool_in_available_names():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    assert "get_mock_loop_retrospective" in AVAILABLE_TOOL_NAMES


@pytest.mark.asyncio
async def test_tool_returns_cycles(monkeypatch):
    import app.mcp_server.tooling.mock_loop_retro_registration as mod

    async def _fake_build(db, *, kst_date_from, kst_date_to, market=None):
        return [{"kst_date": kst_date_from, "triggered": 0}]

    monkeypatch.setattr(mod, "build_mock_loop_retrospective", _fake_build)

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_session_factory", lambda: (lambda: _Ctx()))

    tools = build_tools()
    result = await tools["get_mock_loop_retrospective"](
        kst_date_from="2026-06-02", kst_date_to="2026-06-02"
    )
    assert result["success"] is True
    assert result["cycles"] == [{"kst_date": "2026-06-02", "triggered": 0}]
