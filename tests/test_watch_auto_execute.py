"""ROB-402 — maybe_auto_execute service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert
from app.models.review import WatchOrderIntentLedger
from app.services.investment_reports import watch_auto_execute


def _alert(max_action: dict | None, action_mode="auto_execute_mock"):
    return InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal("55000"),
        threshold_key="55000",
        intent="buy_review",
        action_mode=action_mode,
        rationale="r",
        max_action=max_action or {},
        valid_until=datetime(2026, 12, 31, tzinfo=UTC),
    )


def _good_max_action():
    return {
        "side": "buy",
        "quantity": "10",
        "limit_price": "55000",
        "account_mode": "kis_mock",
    }


async def _intent_for(db, correlation_id):
    return (
        await db.execute(
            select(WatchOrderIntentLedger).where(
                WatchOrderIntentLedger.correlation_id == correlation_id
            )
        )
    ).scalar_one_or_none()


def _make_place_spy():
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)
        return {"success": True, "order_no": "X1"}

    return _spy, calls


@pytest.mark.asyncio
async def test_global_flag_off_blocks(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", False
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert "auto_execute_globally_disabled" in outcome["blocking_reasons"]
    assert calls == []
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "failed"


@pytest.mark.asyncio
async def test_happy_path_places_order(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is True
    assert len(calls) == 1
    assert calls[0]["is_mock"] is True
    assert calls[0]["dry_run"] is False
    assert calls[0]["correlation_id"] == cid
    assert calls[0]["symbol"] == "005930"
    assert calls[0]["side"] == "buy"
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "previewed"
    assert row.execution_allowed is True


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_correlation_id(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    second = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert second["executed"] is False
    assert second.get("skipped") == "duplicate"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_live_account_blocked_no_order(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    alert = _alert({**_good_max_action(), "account_mode": "kis_live"})
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert outcome["blocked_by"] == "live_account"
    assert calls == []
    # no kis_mock intent row written for a live attempt
    assert await _intent_for(db_session, cid) is None


@pytest.mark.asyncio
async def test_failed_broker_result_persists_failed_outcome(db_session, monkeypatch):
    """ROB-843: a failed broker result must not be recorded as executed=True.
    The intent row flips to 'failed' with the reason/detail preserved."""
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    calls = []

    async def _reject(**kwargs):
        calls.append(kwargs)
        return {
            "success": False,
            "status": "rejected",
            "reason": "broker_rejected",
            "detail": "거부",
            "response_message": "거부",
        }

    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=_reject,
    )
    assert outcome["executed"] is False
    assert outcome["reason"] == "broker_rejected"
    assert outcome["detail"] == "거부"
    assert len(calls) == 1  # order WAS attempted
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "failed"
    assert row.execution_allowed is False
    assert row.blocked_by == "broker_rejected"
    assert "broker_rejected" in (row.blocking_reasons or [])


@pytest.mark.asyncio
async def test_malformed_broker_result_persists_failed(db_session, monkeypatch):
    """ROB-843: a non-dict broker result is a failure, never executed=True."""
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )

    async def _weird(**kwargs):
        return None

    alert = _alert(_good_max_action())
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=_weird,
    )
    assert outcome["executed"] is False
    assert outcome["reason"] == "malformed_result"
    row = await _intent_for(db_session, cid)
    assert row.lifecycle_state == "failed"


@pytest.mark.asyncio
async def test_missing_limit_price_blocks(db_session, monkeypatch):
    monkeypatch.setattr(
        watch_auto_execute.settings, "WATCH_AUTO_EXECUTE_MOCK_ENABLED", True
    )
    spy, calls = _make_place_spy()
    ma = _good_max_action()
    ma.pop("limit_price")
    alert = _alert(ma)
    cid = f"corr-{uuid.uuid4().hex}"
    outcome = await watch_auto_execute.maybe_auto_execute(
        db_session,
        alert=alert,
        correlation_id=cid,
        kst_date="2026-06-01",
        place_order_fn=spy,
    )
    assert outcome["executed"] is False
    assert "missing_limit_price" in outcome["blocking_reasons"]
    assert calls == []
    assert (await _intent_for(db_session, cid)).lifecycle_state == "failed"
