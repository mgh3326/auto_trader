# tests/services/test_mirror_counterfactual_plans.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.services.trade_journal.mirror_counterfactual import build_mirror_order_plans


async def _report(db, *, market="kr", account_scope="kis_live") -> InvestmentReport:
    row = InvestmentReport(
        report_uuid=uuid4(),
        idempotency_key=f"rob734-report-{uuid4().hex}",
        title="ROB-734 source report",
        summary="Mirror counterfactual source report",
        report_type="daily",
        market=market,
        market_session="regular",
        account_scope=account_scope,
        execution_mode="advisory_only",
        status="draft",
        created_by_profile="CLAUDE_ADVISOR",
        valid_until=datetime(2026, 7, 6, tzinfo=UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def _item(db, report, **kw):
    base = {
        "report_id": report.id,
        "item_uuid": uuid4(),
        "idempotency_key": f"rob734-item-{uuid4().hex}",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "target_kind": "asset",
        "confidence": Decimal("0.61"),
        "rationale": "original plan",
        "evidence_snapshot": {"trade_setup": {"target": 76000, "stop": 68000}},
        "trigger_checklist": [],
        "max_action": {"quantity": "3", "limit_price": "70000"},
        "status": "denied",
        "decision_bucket": "new_buy_candidate",
    }
    base.update(kw)
    row = InvestmentReportItem(**base)
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_action_item_uses_original_max_action_even_when_denied(db_session):
    report = await _report(db_session)
    item = await _item(db_session, report, status="denied")
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.item_uuid == item.item_uuid
    assert plan.source_bucket == "place_original"
    assert plan.quantity == Decimal("3")
    assert plan.price == Decimal("70000")
    assert plan.amount is None
    assert plan.target_price == Decimal("76000")
    assert plan.stop_loss == Decimal("68000")


@pytest.mark.asyncio
async def test_watch_item_uses_watch_threshold_price(db_session):
    report = await _report(db_session)
    item = await _item(
        db_session,
        report,
        item_kind="watch",
        operation="create",
        watch_condition={
            "metric": "price",
            "operator": "below",
            "threshold": "69000",
        },
        valid_until=datetime(2026, 7, 7, tzinfo=UTC),
        max_action={"side": "buy", "quantity": "2", "account_mode": "kis_mock"},
    )
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.item_uuid == item.item_uuid
    assert plan.source_bucket == "watch_trigger"
    assert plan.price == Decimal("69000")
    assert plan.quantity == Decimal("2")


@pytest.mark.asyncio
async def test_deferred_no_action_gets_minimum_rung(db_session):
    report = await _report(db_session)
    await _item(
        db_session,
        report,
        item_kind="action",
        decision_bucket="deferred_no_action",
        max_action={},
        evidence_snapshot={"price": "12345"},
    )
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    [plan] = result["plans"]
    assert plan.source_bucket == "deferred_min_rung"
    assert plan.quantity == Decimal("1")
    assert plan.price == Decimal("12345")


@pytest.mark.asyncio
async def test_item_without_price_is_skipped_with_reason(db_session):
    report = await _report(db_session)
    item = await _item(
        db_session, report, max_action={"quantity": "1"}, evidence_snapshot={}
    )
    await db_session.commit()

    result = await build_mirror_order_plans(db_session, report_uuid=report.report_uuid)
    assert result["plans"] == []
    assert result["skipped"][0]["item_uuid"] == str(item.item_uuid)
    assert result["skipped"][0]["reason"] == "missing_limit_price"
