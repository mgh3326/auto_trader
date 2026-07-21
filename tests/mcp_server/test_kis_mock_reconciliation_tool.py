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
    # ROB-1018 fix #2 (round 2): every path with a valid scope echoes the
    # effective/canonical scope — market="us" normalizes to "equity_us" here
    # exactly like the success path does, not the raw pre-alias request.
    assert result["scope"] == {"market": "equity_us", "symbol": "AVGO"}


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
    # ROB-1018 fix #2 (round 2): no valid scope was ever established, so this
    # path must NOT claim a `scope` (that key means "effective scope of what
    # ran/would run" on every other path) — it echoes the verbatim request
    # under `requested_scope` instead, so callers can't mistake it for a
    # scope that actually executed.
    assert result["requested_scope"] == {"market": "crypto", "symbol": None}
    assert "scope" not in result
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
    # ROB-1018 fix #2 (round 2): rejected requests have no effective scope —
    # they echo the raw request under `requested_scope`, not `scope`.
    assert result["requested_scope"]["market"] == "krx"
    assert "scope" not in result


@pytest.mark.asyncio
async def test_scope_contract_consistent_across_all_paths_for_market_us(monkeypatch):
    """ROB-1018 fix #2 (round 2), TDD requirement #1.

    For the same input (market="us", symbol="AVGO"), every path that has a
    valid scope — config error, confirm-required, success, and an
    impl/DB exception — must agree on the SAME normalization rule (the
    effective/canonical alias-resolved value, "equity_us"), never the raw
    pre-alias request. The one path without a valid scope (unknown market)
    is the sole, explicitly-signposted exception: it must never claim
    `scope` and instead echoes the verbatim request under `requested_scope`.
    """
    from app.mcp_server.tooling import kis_mock_ledger

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _BoomDB:
        async def __aenter__(self):
            raise RuntimeError("db unavailable")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )
    tools = build_tools()

    # 1) success path — the job-level reconciliation is stubbed out, but the
    # front-end scope contract only depends on the impl's own normalization.
    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: _FakeDB()
    )
    monkeypatch.setattr(
        kis_mock_ledger,
        "run_kis_mock_reconciliation",
        AsyncMock(
            return_value={
                "success": True,
                "scope": {"market": "equity_us", "symbol": "AVGO"},
            }
        ),
    )
    success = await tools["kis_mock_reconciliation_run"](market="us", symbol="AVGO")
    assert success["scope"] == {"market": "equity_us", "symbol": "AVGO"}

    # 2) confirm=False short-circuit — never reaches impl at all.
    confirm_required = await tools["kis_mock_reconciliation_run"](
        dry_run=False, confirm=False, market="us", symbol="AVGO"
    )
    assert confirm_required["success"] is False
    assert confirm_required["scope"] == {"market": "equity_us", "symbol": "AVGO"}

    # 3) config-error short-circuit — never reaches impl at all.
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: ["KIS_MOCK_APP_KEY"],
    )
    config_err = await tools["kis_mock_reconciliation_run"](market="us", symbol="AVGO")
    assert config_err["success"] is False
    assert config_err["scope"] == {"market": "equity_us", "symbol": "AVGO"}
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    # 4) impl/DB exception path.
    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: _BoomDB()
    )
    boom = await tools["kis_mock_reconciliation_run"](market="us", symbol="AVGO")
    assert boom["success"] is False
    assert boom["scope"] == {"market": "equity_us", "symbol": "AVGO"}

    # 5) unknown-market rejection — the deliberate exception to the rule
    # above: no `scope` key, verbatim request under `requested_scope`.
    rejected = await tools["kis_mock_reconciliation_run"](market="crypto")
    assert rejected["success"] is False
    assert "scope" not in rejected
    assert rejected["requested_scope"] == {"market": "crypto", "symbol": None}


def test_tool_description_matches_scope_contract():
    """ROB-1018 fix #2 (round 2), TDD requirement #2: the tool's own
    description must describe the actual scope contract, not the stale
    "always echoes the requested scope" claim that round 1 shipped."""
    from app.mcp_server.tooling.orders_registration import register_order_tools

    descriptions: dict[str, str] = {}

    class _DescriptionCapturingMCP:
        def tool(self, name: str, description: str):
            def decorator(func):
                descriptions[name] = description
                return func

            return decorator

    register_order_tools(_DescriptionCapturingMCP())
    description = descriptions["kis_mock_reconciliation_run"]

    assert "effective" in description or "canonical" in description
    assert "requested_scope" in description
    # The stale round-1 claim ("always echoes the requested scope" as the
    # verbatim input) must be gone now that success/error paths normalize.
    assert "always echoes the requested" not in description.lower()
