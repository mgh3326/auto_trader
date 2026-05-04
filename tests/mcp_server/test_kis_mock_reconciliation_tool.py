"""Smoke tests for KIS mock reconciliation tool (ROB-102)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_dry_run_default(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    mock_run = AsyncMock(return_value={"success": True, "applied": 0})
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)

    tools = build_tools()
    assert "kis_mock_reconciliation_run" in tools

    result = await tools["kis_mock_reconciliation_run"]()
    assert result == {"success": True, "applied": 0}
    mock_run.assert_awaited_once_with(dry_run=True, limit=100)


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_apply_requires_confirm(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    mock_run = AsyncMock()
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)

    tools = build_tools()
    result = await tools["kis_mock_reconciliation_run"](
        dry_run=False, confirm=False, limit=10
    )

    assert result["success"] is False
    assert "confirm" in result["error"].lower()
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_apply_with_confirm(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    mock_run = AsyncMock(return_value={"success": True, "applied": 3})
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)

    tools = build_tools()
    result = await tools["kis_mock_reconciliation_run"](
        dry_run=False, confirm=True, limit=10
    )

    assert result == {"success": True, "applied": 3}
    mock_run.assert_awaited_once_with(dry_run=False, limit=10)
