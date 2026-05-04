"""Tests for KISMockLifecycleService (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
from app.services.kis_mock_lifecycle_service import (
    KISMockLifecycleService,
    LedgerNotFoundError,
)

SERVICE_PATH = Path(__file__).parents[2] / "app/services/kis_mock_lifecycle_service.py"


@pytest_asyncio.fixture
async def seeded_ledger_id(db_session: AsyncSession) -> int:
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("10"),
        price=Decimal("70000"),
        amount=Decimal("700000"),
        currency="KRW",
        order_no="MOCK-1",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        holdings_baseline_qty=Decimal("0"),
    )
    db_session.add(row)
    await db_session.commit()
    return row.id


@pytest.mark.asyncio
async def test_apply_lifecycle_transition_dry_run_does_not_persist(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    summary = await svc.apply_lifecycle_transition(
        ledger_id=seeded_ledger_id,
        next_state="fill",
        reason_code="fill_detected",
        detail={"observed_holdings_qty": "10", "observed_delta": "10"},
        dry_run=True,
    )
    assert summary["dry_run"] is True
    assert summary["would_change"] is True
    assert summary["next_state"] == "fill"

    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.lifecycle_state == "accepted"  # unchanged
    assert row.reconcile_attempts == 0
    assert row.last_reconcile_detail is None


@pytest.mark.asyncio
async def test_apply_lifecycle_transition_writes_when_not_dry_run(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    summary = await svc.apply_lifecycle_transition(
        ledger_id=seeded_ledger_id,
        next_state="fill",
        reason_code="fill_detected",
        detail={"observed_delta": "10"},
        dry_run=False,
    )
    assert summary["applied"] is True

    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.lifecycle_state == "fill"
    assert row.reconcile_attempts == 1
    assert row.last_reconcile_detail == {
        "reason_code": "fill_detected",
        "observed_delta": "10",
    }


@pytest.mark.asyncio
async def test_apply_lifecycle_transition_records_reconciled_at_only_for_terminal(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    await svc.apply_lifecycle_transition(
        ledger_id=seeded_ledger_id,
        next_state="reconciled",
        reason_code="position_reconciled",
        detail={},
        dry_run=False,
    )
    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.lifecycle_state == "reconciled"
    assert row.reconciled_at is not None


@pytest.mark.asyncio
async def test_apply_lifecycle_transition_unknown_id_raises(
    db_session: AsyncSession,
):
    svc = KISMockLifecycleService(db_session)
    with pytest.raises(LedgerNotFoundError):
        await svc.apply_lifecycle_transition(
            ledger_id=9_999_999,
            next_state="pending",
            reason_code="pending_unconfirmed",
            detail={},
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_record_holdings_baseline(
    db_session: AsyncSession, seeded_ledger_id: int
):
    svc = KISMockLifecycleService(db_session)
    await svc.record_holdings_baseline(
        ledger_id=seeded_ledger_id, baseline_qty=Decimal("3")
    )
    row = await db_session.get(KISMockOrderLedger, seeded_ledger_id)
    assert row.holdings_baseline_qty == Decimal("3")


@pytest.mark.asyncio
async def test_list_open_orders_returns_only_inflight_and_fill(
    db_session: AsyncSession, seeded_ledger_id: int
):
    # add a terminal row that should be excluded
    terminal = KISMockOrderLedger(
        trade_date=datetime(2026, 5, 3, tzinfo=UTC),
        symbol="000660",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("100"),
        amount=Decimal("100"),
        currency="KRW",
        order_no="MOCK-2",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="reconciled",
    )
    db_session.add(terminal)
    await db_session.commit()

    svc = KISMockLifecycleService(db_session)
    rows = await svc.list_open_orders(limit=50)
    ids = {r.id for r in rows}
    assert seeded_ledger_id in ids
    assert terminal.id not in ids


def test_service_does_not_call_broker_or_live_paths():
    """Service must remain record-keeping only. No broker / live imports."""
    src = SERVICE_PATH.read_text()
    forbidden = [
        "KISClient",
        "from app.services.brokers",
        "from app.services.order_execution",
        "from app.services.trade_journal",
        "from app.services.fill_notification",
        "from app.tasks.kis",
    ]
    for tok in forbidden:
        assert tok not in src, f"forbidden import: {tok}"
