# tests/services/test_mirror_counterfactual_execution.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.review import KISMockOrderLedger
from app.services.trade_journal.mirror_counterfactual import (
    MirrorOrderPlan,
    execute_mirror_order_plans,
)


def _plan() -> MirrorOrderPlan:
    item_uuid = uuid4()
    return MirrorOrderPlan(
        report_uuid=uuid4(),
        item_uuid=item_uuid,
        source_bucket="place_original",
        correlation_id=f"mirror:{item_uuid}",
        symbol="005930",
        side="buy",
        quantity=Decimal("2"),
        amount=None,
        price=Decimal("70000"),
        target_price=Decimal("76000"),
        stop_loss=Decimal("68000"),
        min_hold_days=10,
        reason="ROB-734 mirror counterfactual",
        thesis="original plan",
        strategy="mirror_counterfactual",
        notes="source_bucket=place_original",
    )


@pytest.mark.asyncio
async def test_execute_dry_run_calls_place_order_without_metadata_write(db_session):
    calls = []

    async def fake_place_order(**kwargs):
        calls.append(kwargs)
        return {"success": True, "dry_run": True, "approval_hash": "p6a1.x"}

    result = await execute_mirror_order_plans(
        db_session,
        plans=[_plan()],
        dry_run=True,
        place_order=fake_place_order,
    )

    assert result["submitted_count"] == 0
    assert result["dry_run_count"] == 1
    assert calls[0]["is_mock"] is True
    assert calls[0]["dry_run"] is True
    assert calls[0]["correlation_id"].startswith("mirror:")


@pytest.mark.asyncio
async def test_execute_apply_stamps_mock_ledger_metadata(db_session):
    plan = _plan()
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 7, 6, tzinfo=UTC),
        symbol=plan.symbol,
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        price=Decimal("70000"),
        amount=Decimal("140000"),
        fee=Decimal("0"),
        currency="KRW",
        order_no=f"ROB734-{uuid4().hex[:10]}",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state="accepted",
        correlation_id=plan.correlation_id,
    )
    db_session.add(row)
    await db_session.flush()
    ledger_id = row.id

    async def fake_place_order(**kwargs):
        return {"success": True, "dry_run": False, "ledger_id": ledger_id}

    result = await execute_mirror_order_plans(
        db_session,
        plans=[plan],
        dry_run=False,
        place_order=fake_place_order,
    )
    await db_session.commit()

    assert result["submitted_count"] == 1
    refreshed = await db_session.scalar(
        select(KISMockOrderLedger).where(KISMockOrderLedger.id == ledger_id)
    )
    assert refreshed.report_item_uuid == plan.item_uuid
    assert refreshed.mirror_cohort == "mock_counterfactual"
    assert refreshed.mirror_source_bucket == "place_original"
