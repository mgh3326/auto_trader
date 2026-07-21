"""ROB-1007 R4 follow-up: job-layer chokepoint bypass fix.

``run_kis_mock_reconciliation`` (the job function) used to build
``scope = {"market": market, "symbol": symbol}`` directly, bypassing
``resolve_kis_mock_reconcile_scope`` / the allowlist entirely. That meant an
invalid ``market`` at the job layer (reachable by any direct caller, not just
the MCP tool) silently produced a false ``success=True, orders_processed=0``
instead of failing closed — exactly the class of defect ROB-1018 fixed at the
MCP-tool layer, reintroduced one layer down. These tests pin the RED case
(before the fix: ``success=True``) to now be GREEN (``success=False``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation


@pytest.mark.asyncio
async def test_job_layer_rejects_unknown_market_instead_of_false_success(monkeypatch):
    """RED case from the task brief: calling the job function directly with
    an unrecognized market must fail closed, not silently no-op-succeed."""
    fake_lifecycle_svc = AsyncMock()
    fake_lifecycle_svc.list_open_orders.return_value = []
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _db: fake_lifecycle_svc,
    )

    result = await run_kis_mock_reconciliation(db=MagicMock(), market="crypto-x")

    # Before the fix this was {"success": True, "orders_processed": 0,
    # "scope": {"market": "crypto-x"}} — a false success for an invalid
    # market. It must now be an explicit rejection.
    assert result["success"] is False
    assert result["selector"] == "market"
    assert "crypto-x" in result["error"]
    assert "scope" not in result
    assert result["requested_scope"] == {"market": "crypto-x", "symbol": None}
    # Never reaches the open-orders query at all.
    fake_lifecycle_svc.list_open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_job_layer_accepts_valid_market_unchanged(monkeypatch):
    """Non-regression: a valid market still normalizes and runs exactly as
    before the chokepoint was wired in."""
    mock_ledger_row = MagicMock()
    mock_ledger_row.id = 1
    mock_ledger_row.symbol = "AVGO"
    mock_ledger_row.side = "buy"
    mock_ledger_row.quantity = Decimal("1")
    mock_ledger_row.lifecycle_state = "accepted"
    mock_ledger_row.holdings_baseline_qty = Decimal("0")
    mock_ledger_row.trade_date = datetime.now(UTC) - timedelta(seconds=10)

    fake_lifecycle_svc = AsyncMock()
    fake_lifecycle_svc.list_open_orders.return_value = [mock_ledger_row]
    fake_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": False}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _db: fake_lifecycle_svc,
    )

    fake_kis = MagicMock()
    fake_kis.fetch_my_stocks = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISClient",
        lambda *a, **kw: fake_kis,
    )

    result = await run_kis_mock_reconciliation(db=MagicMock(), market="us")

    assert result["success"] is True
    assert result["scope"] == {"market": "equity_us", "symbol": None}
    fake_lifecycle_svc.list_open_orders.assert_awaited_once_with(
        limit=100, symbol=None, instrument_type="equity_us", ledger_ids=None
    )


@pytest.mark.asyncio
async def test_job_layer_rejects_invalid_ledger_ids(monkeypatch):
    fake_lifecycle_svc = AsyncMock()
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _db: fake_lifecycle_svc,
    )

    result = await run_kis_mock_reconciliation(db=MagicMock(), ledger_ids=[])

    assert result["success"] is False
    assert result["selector"] == "ledger_ids"
    assert "scope" not in result
    fake_lifecycle_svc.list_open_orders.assert_not_called()
    fake_lifecycle_svc.existing_ledger_ids.assert_not_called()


@pytest.mark.asyncio
async def test_job_layer_rejects_nonexistent_ledger_ids(monkeypatch):
    """A structurally-valid but nonexistent ledger_id must be rejected
    explicitly rather than silently processing zero rows as a success."""
    fake_lifecycle_svc = AsyncMock()
    fake_lifecycle_svc.existing_ledger_ids.return_value = {1}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _db: fake_lifecycle_svc,
    )

    result = await run_kis_mock_reconciliation(db=MagicMock(), ledger_ids=[1, 999])

    assert result["success"] is False
    assert result["selector"] == "ledger_ids"
    assert "999" in result["error"]
    assert result["scope"] == {"market": None, "symbol": None, "ledger_ids": [1, 999]}
    fake_lifecycle_svc.existing_ledger_ids.assert_awaited_once_with([1, 999])
    fake_lifecycle_svc.list_open_orders.assert_not_called()


@pytest.mark.asyncio
async def test_job_layer_scopes_open_orders_to_valid_ledger_ids(monkeypatch):
    fake_lifecycle_svc = AsyncMock()
    fake_lifecycle_svc.existing_ledger_ids.return_value = {1, 2}
    fake_lifecycle_svc.list_open_orders.return_value = []
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _db: fake_lifecycle_svc,
    )

    result = await run_kis_mock_reconciliation(db=MagicMock(), ledger_ids=[2, 1])

    assert result["success"] is True
    assert result["scope"] == {"market": None, "symbol": None, "ledger_ids": [2, 1]}
    fake_lifecycle_svc.list_open_orders.assert_awaited_once_with(
        limit=100, symbol=None, instrument_type=None, ledger_ids=[2, 1]
    )
