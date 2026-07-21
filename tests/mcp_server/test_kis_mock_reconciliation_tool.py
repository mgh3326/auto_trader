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
    mock_run.assert_awaited_once_with(dry_run=True, limit=100, market=None, symbol=None)


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
        dry_run=False, confirm=False, limit=10, market="us", symbol="AVGO"
    )

    assert result["success"] is False
    assert "confirm" in result["error"].lower()
    mock_run.assert_not_called()
    # ROB-1018 fix #2: the confirm=False short-circuit must still echo the
    # scope the caller requested — callers should be able to tell what was
    # (not) targeted from every response shape, not just the success path.
    assert result["scope"] == {"market": "us", "symbol": "AVGO"}


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_config_error_includes_scope(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: ["KIS_MOCK_APP_KEY"],
    )

    mock_run = AsyncMock()
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)

    tools = build_tools()
    result = await tools["kis_mock_reconciliation_run"](
        market="equity_kr", symbol="005930"
    )

    assert result["success"] is False
    mock_run.assert_not_called()
    # ROB-1018 fix #2: a config-error short-circuit (before impl is ever
    # reached) must still echo the requested scope.
    assert result["scope"] == {"market": "equity_kr", "symbol": "005930"}


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
    mock_run.assert_awaited_once_with(dry_run=False, limit=10, market=None, symbol=None)


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_passes_market_and_symbol(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    mock_run = AsyncMock(return_value={"success": True, "applied": 0})
    monkeypatch.setattr(kis_mock_ledger, "kis_mock_reconciliation_run_impl", mock_run)

    tools = build_tools()
    result = await tools["kis_mock_reconciliation_run"](
        market="us", symbol="AVGO", limit=10
    )

    assert result == {"success": True, "applied": 0}
    mock_run.assert_awaited_once_with(
        dry_run=True, limit=10, market="us", symbol="AVGO"
    )


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_run_impl_exception_includes_scope(
    monkeypatch,
):
    """ROB-1018 fix #2: a raised exception inside the impl's own try/except
    (e.g. a DB session failure) must still surface the requested scope so a
    caller can tell what was being reconciled when it blew up."""
    from app.mcp_server.tooling import kis_mock_ledger

    class _BoomDB:
        async def __aenter__(self):
            raise RuntimeError("db unavailable")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: _BoomDB()
    )

    result = await kis_mock_ledger.kis_mock_reconciliation_run_impl(
        market="us", symbol="AVGO"
    )

    assert result["success"] is False
    assert result["error"]
    assert result["scope"] == {"market": "equity_us", "symbol": "AVGO"}


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_run_impl_rejects_unknown_market(monkeypatch):
    """ROB-1018 fix #3: an unrecognized market must fail closed with an
    explicit error + allowed-values list, never silently succeed as if it
    scanned nothing on purpose (a `crypto`/typo value must not read as a
    trustworthy 'scope matched, 0 orders' success)."""
    from app.mcp_server.tooling import kis_mock_ledger

    run_spy = AsyncMock()
    monkeypatch.setattr(kis_mock_ledger, "run_kis_mock_reconciliation", run_spy)

    result = await kis_mock_ledger.kis_mock_reconciliation_run_impl(market="crypto")

    assert result["success"] is False
    assert "crypto" in result["error"]
    assert set(result["allowed_markets"]) == {"kr", "us", "equity_kr", "equity_us"}
    assert result["scope"] == {"market": "crypto", "symbol": None}
    # Must fail closed BEFORE ever reaching the reconciliation query/holdings
    # fetch — never a silent full-scan fallback for a bad market value.
    run_spy.assert_not_called()


@pytest.mark.asyncio
async def test_kis_mock_reconciliation_tool_rejects_unknown_market_end_to_end(
    monkeypatch,
):
    """Same as above but through the registered MCP tool surface."""
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    tools = build_tools()
    result = await tools["kis_mock_reconciliation_run"](market="krx")

    assert result["success"] is False
    assert set(result["allowed_markets"]) == {"kr", "us", "equity_kr", "equity_us"}
    assert result["scope"]["market"] == "krx"
