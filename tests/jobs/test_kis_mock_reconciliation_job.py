"""Tests for KIS mock reconciliation job composition (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation
from app.mcp_server.tooling.kis_mock_ledger import _shadow_row_to_order
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


@pytest.mark.asyncio
async def test_reconciliation_attributes_single_delta_and_records_attributed_qty(
    monkeypatch,
):
    row23 = _ledger_row(
        ledger_id=23,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=120,
    )
    row23.price = Decimal("15500")

    row24 = _ledger_row(
        ledger_id=24,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=60,
    )
    row24.price = Decimal("15900")

    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [row23, row24]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "0148J0", "hldg_qty": "10"}])

    result = await run_kis_mock_reconciliation(
        mock_db, dry_run=True, kis_client=fake_kis
    )

    events = {e["detail"]["ledger_id"]: e for e in result["events"]}
    assert events[24]["state"] == "fill"
    assert events[23]["state"] == "pending"

    # attributed_fill_qty is recorded in the applied detail / event payload
    assert events[24]["detail"]["attributed_fill_qty"] == "10"
    assert events[23]["detail"]["attributed_fill_qty"] == "0"


@pytest.mark.asyncio
async def test_attributed_fill_qty_roundtrips_into_shadow_order_history(monkeypatch):
    """Cross-seam (ROB-400 Fix #3): the exact detail the job hands the
    persistence layer (str(Decimal) ``attributed_fill_qty``) is read back by the
    shadow order-history reader without contradicting ``lifecycle_state``.

    This closes the writer/reader contract — a rename on either side breaks it,
    whereas the isolated reconciler and shadow tests would each still pass.
    """
    row24 = _ledger_row(
        ledger_id=24,
        symbol="0148J0",
        side="buy",
        qty=Decimal("10"),
        state="accepted",
        baseline=Decimal("0"),
        accepted_age_sec=60,
    )
    row24.price = Decimal("15900")

    mock_db = AsyncMock()
    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [row24]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {"applied": True}
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = _fake_kis_client(kr=[{"pdno": "0148J0", "hldg_qty": "10"}])

    await run_kis_mock_reconciliation(mock_db, dry_run=False, kis_client=fake_kis)

    # Reconstruct exactly what KISMockLifecycleService.apply_lifecycle_transition
    # persists into row.last_reconcile_detail: {"reason_code", **detail}.
    call = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    persisted_detail = {"reason_code": call["reason_code"], **call["detail"]}

    shadow_row = MagicMock(spec=KISMockOrderLedger)
    shadow_row.id = 24
    shadow_row.order_no = None
    shadow_row.symbol = "0148J0"
    shadow_row.instrument_type = "equity_kr"
    shadow_row.side = "buy"
    shadow_row.order_type = "limit"
    shadow_row.quantity = Decimal("10")
    shadow_row.price = Decimal("15900")
    shadow_row.amount = Decimal("159000")
    shadow_row.currency = "KRW"
    shadow_row.trade_date = datetime.now(UTC)
    shadow_row.lifecycle_state = call["next_state"]
    shadow_row.last_reconcile_detail = persisted_detail

    out = _shadow_row_to_order(shadow_row)
    assert out["lifecycle_state"] == "fill"
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0


@pytest.mark.asyncio
async def test_run_passes_symbol_to_list_open_orders(db_session, monkeypatch):
    from app.services.kis_mock_lifecycle_service import KISMockLifecycleService

    captured: dict = {}

    async def _fake_list_open_orders(self, *, limit=100, symbol=None, **kw):
        captured["symbol"] = symbol
        captured["limit"] = limit
        return []  # empty → run short-circuits before broker/holdings

    monkeypatch.setattr(
        KISMockLifecycleService, "list_open_orders", _fake_list_open_orders
    )
    result = await run_kis_mock_reconciliation(
        db_session, symbol="005930", dry_run=True
    )
    assert captured["symbol"] == "005930"
    assert result["orders_processed"] == 0


def test_reconcile_gate_flags_default_false():
    from app.core.config import settings

    assert settings.KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED is False
    assert settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED is False
