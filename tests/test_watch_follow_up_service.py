"""ROB-405 Slice E — watch follow-up link."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem, InvestmentWatchEvent
from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.trade_journal import watch_follow_up_service as svc

# Seeds + commits investment_reports / watch_events on the shared test DB —
# hold the cleanup lock so a concurrent xdist TRUNCATE can't wipe rows mid-test
# (ROB-375 / Slice D lesson).
pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")


async def _event(db, *, cid, kst_date="2099-01-02", market="kr", symbol="005930"):
    ev = InvestmentWatchEvent(
        event_uuid=uuid4(),
        idempotency_key=f"idem-{uuid4()}",
        source_report_uuid=uuid4(),
        source_item_uuid=uuid4(),
        market=market,
        target_kind="asset",
        symbol=symbol,
        metric="price",
        operator="below",
        threshold=Decimal("49000"),
        threshold_key="49000",
        intent="buy_review",
        action_mode="auto_execute_mock",
        outcome="executed",
        current_value=Decimal("49500"),
        correlation_id=cid,
        kst_date=kst_date,
    )
    db.add(ev)
    await db.commit()
    return ev


async def _closed_mock_journal_with_verdict(db, *, cid, pnl="5", verdict="good"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("52500"),
        exit_date=datetime(2099, 1, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl),
    )
    db.add(j)
    await db.commit()
    db.add(TradeJournalReview(journal_id=j.id, verdict=verdict, verdict_source="auto"))
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_repo_update_event_follow_up(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    # need any item id; reuse the journal's report-less context via a raw item is
    # not possible (report_id NOT NULL) — instead assert FK set to an existing
    # item created in the service test. Here assert the writer issues the update.
    repo = InvestmentReportsRepository(db_session)
    # create a placeholder report+item via repo to get a valid item id
    report = await repo.insert_report(
        report_uuid=uuid4(),
        idempotency_key=f"rk-{uuid4()}",
        report_type="mock_loop_followup",
        market="kr",
        execution_mode="mock_preview",
        account_scope="kis_mock",
        created_by_profile="t",
        title="t",
        summary="s",
        status="draft",
    )
    item = await repo.insert_item(
        item_uuid=uuid4(),
        idempotency_key=f"ik-{uuid4()}",
        report_id=report.id,
        item_kind="watch",
        operation="review",
        symbol="005930",
        intent="trend_recovery_review",
        target_kind="asset",
        rationale="r",
        evidence_snapshot={"correlation_id": cid},
    )
    await db_session.commit()
    await repo.update_event_follow_up(ev.id, follow_up_report_item_id=item.id)
    await db_session.commit()
    await db_session.refresh(ev)
    assert ev.follow_up_report_item_id == item.id


async def _item_for_event(db, ev):
    await db.refresh(ev)
    if ev.follow_up_report_item_id is None:
        return None
    return await db.get(InvestmentReportItem, ev.follow_up_report_item_id)


@pytest.mark.asyncio
async def test_sync_links_eligible_event(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid, verdict="good")
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["linked"] == 1
    item = await _item_for_event(db_session, ev)
    assert item is not None
    assert item.operation == "review"
    assert item.evidence_snapshot["correlation_id"] == cid


@pytest.mark.asyncio
async def test_sync_skips_event_without_verdict(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    # closed mock journal but NO review
    j = TradeJournal(
        symbol="005930", instrument_type="equity_kr", side="buy",
        entry_price=Decimal("50000"), quantity=Decimal("10"), thesis="t",
        account_type="mock", account="kis_mock", correlation_id=cid,
        status="closed", pnl_pct=Decimal("5"),
        exit_price=Decimal("52500"), exit_date=datetime(2099, 1, 2, tzinfo=UTC),
    )
    db_session.add(j)
    await db_session.commit()
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["linked"] == 0
    assert await _item_for_event(db_session, ev) is None


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    await svc.sync_watch_follow_up_items(db_session)
    first_item = await _item_for_event(db_session, ev)
    out2 = await svc.sync_watch_follow_up_items(db_session)
    assert out2["linked"] == 0  # already linked → skipped
    second_item = await _item_for_event(db_session, ev)
    assert second_item.id == first_item.id


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", False)
    cid = f"corr-{uuid4().hex}"
    await _closed_mock_journal_with_verdict(db_session, cid=cid)
    ev = await _event(db_session, cid=cid)
    out = await svc.sync_watch_follow_up_items(db_session)
    assert out["status"] == "disabled"
    assert await _item_for_event(db_session, ev) is None
