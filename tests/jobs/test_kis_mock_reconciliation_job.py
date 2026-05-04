"""Tests for KIS mock reconciliation job composition (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.models.review import KISMockOrderLedger


def _ledger_row(
    *,
    ledger_id: int = 101,
    symbol: str = "005930",
    side: str = "buy",
    qty: Decimal = Decimal("10"),
    state: str = "accepted",
    baseline: Decimal | None = Decimal("5"),
    accepted_age_sec: int = 5,
):
    row = MagicMock(spec=KISMockOrderLedger)
    row.id = ledger_id
    row.symbol = symbol
    row.side = side
    row.quantity = qty
    row.lifecycle_state = state
    row.holdings_baseline_qty = baseline
    row.trade_date = datetime.now(UTC) - timedelta(seconds=accepted_age_sec)
    return row


def _fake_kis_client(*, kr=None, us=None):
    client = MagicMock()
    client.fetch_my_stocks = AsyncMock(side_effect=[kr or [], us or []])
    return client


@pytest.mark.asyncio
async def test_reconciliation_job_uses_kis_mock_holdings(monkeypatch):
    """Job must call fetch_my_stocks(is_mock=True) for both KR and US."""
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [_ledger_row()]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    # Real KIS contract: KR uses pdno/hldg_qty, US uses ovrs_pdno/ovrs_cblc_qty.
    fake_kis = _fake_kis_client(kr=[{"pdno": "005930", "hldg_qty": "15"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=False, kis_client=fake_kis
    )

    assert result["orders_processed"] == 1
    assert result["transitions_applied"] == 1
    assert result["account_mode"] == "kis_mock"

    # Verify both KR and US holdings calls were issued with is_mock=True.
    assert fake_kis.fetch_my_stocks.await_count == 2
    kr_call = fake_kis.fetch_my_stocks.await_args_list[0]
    us_call = fake_kis.fetch_my_stocks.await_args_list[1]
    assert kr_call.kwargs == {"is_mock": True, "is_overseas": False}
    assert us_call.kwargs == {"is_mock": True, "is_overseas": True}

    # Verify transition was applied to the right ledger row.
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 101
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert args["dry_run"] is False


@pytest.mark.asyncio
async def test_reconciliation_job_handles_overseas_holdings(monkeypatch):
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [
        _ledger_row(ledger_id=202, symbol="AAPL")
    ]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(
        kr=[],
        us=[{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "15"}],
    )

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=False, kis_client=fake_kis
    )

    assert result["transitions_applied"] == 1
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["next_state"] == "fill"


@pytest.mark.asyncio
async def test_reconciliation_job_emits_lifecycle_events(monkeypatch):
    mock_db = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [_ledger_row()]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "005930", "hldg_qty": "15"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )

    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["account_mode"] == "kis_mock"
    assert event["execution_source"] == "reconciler"
    assert event["state"] == "fill"
    assert event["detail"]["ledger_id"] == 101


@pytest.mark.asyncio
async def test_reconciliation_job_no_open_orders(monkeypatch):
    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = []
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )
    fake_kis = _fake_kis_client()

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )
    assert result["orders_processed"] == 0
    fake_kis.fetch_my_stocks.assert_not_called()
