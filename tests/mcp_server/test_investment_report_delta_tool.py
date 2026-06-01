# tests/mcp_server/test_investment_report_delta_tool.py
"""ROB-376 — investment_report_delta_get handler + registration."""

from __future__ import annotations

import pytest

import app.mcp_server.tooling.investment_reports_handlers as handlers


def test_delta_tool_name_registered():
    assert "investment_report_delta_get" in handlers.INVESTMENT_REPORT_TOOL_NAMES


def test_register_investment_report_tools_includes_delta():
    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)
            return lambda fn: fn

    handlers.register_investment_report_tools(_FakeMCP())
    assert "investment_report_delta_get" in registered


@pytest.mark.asyncio
async def test_delta_impl_invalid_uuid_returns_error():
    out = await handlers.investment_report_delta_get_impl(report_uuid="not-a-uuid")
    assert out == {"success": False, "error": "invalid_report_uuid"}
