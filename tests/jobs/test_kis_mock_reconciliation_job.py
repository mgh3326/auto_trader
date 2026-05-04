"""Tests for KIS mock reconciliation job composition (ROB-102)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.models.review import KISMockOrderLedger
from app.services.kis_mock_holdings_reconciler import (
    HoldingsSnapshot,
    LifecycleTransitionProposal,
)


@pytest.mark.asyncio
async def test_reconciliation_job_composition(monkeypatch):
    # 1. Mock DB session
    mock_db = AsyncMock()
    
    # 2. Mock KISMockLifecycleService
    mock_ledger_row = MagicMock(spec=KISMockOrderLedger)
    mock_ledger_row.id = 101
    mock_ledger_row.symbol = "005930"
    mock_ledger_row.side = "buy"
    mock_ledger_row.quantity = Decimal("10")
    mock_ledger_row.lifecycle_state = "accepted"
    mock_ledger_row.holdings_baseline_qty = Decimal("5")
    mock_ledger_row.trade_date = MagicMock()
    
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [mock_ledger_row]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )
    
    # 3. Mock KISClient (Broker) for holdings
    mock_kis = AsyncMock()
    mock_kis.fetch_my_stocks.return_value = [
        {"symbol": "005930", "qty": "15"}
    ]
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.kis",
        mock_kis,
    )
    
    # 4. Run job (don't mock pure reconciler)
    results = await run_kis_mock_reconciliation(mock_db, dry_run=False)
    
    # Verify composition
    assert results["orders_processed"] == 1
    assert results["transitions_applied"] == 1
    
    # Verify transition was applied
    mock_lifecycle_svc.apply_lifecycle_transition.assert_awaited_once()
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 101
    assert args["next_state"] == "fill"
    assert args["dry_run"] is False
