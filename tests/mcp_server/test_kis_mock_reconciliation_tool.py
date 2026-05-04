"""Smoke tests for KIS mock reconciliation tool (ROB-102)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_available_and_calls_impl(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger
    
    # 1. Mock config to pass
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )
    
    # 2. Mock implementation
    mock_run = AsyncMock(return_value={"success": True, "applied": 5})
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)
    
    # 3. Build tools
    tools = build_tools()
    assert "kis_mock_reconciliation_run" in tools
    
    # 4. Call tool
    result = await tools["kis_mock_reconciliation_run"](dry_run=True, limit=10)
    
    # 5. Verify
    assert result == {"success": True, "applied": 5}
    mock_run.assert_awaited_once_with(dry_run=True, limit=10)
