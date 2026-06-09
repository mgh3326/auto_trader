"""ROB-405 Slice C — journal counterfactual."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual
from app.models.trade_journal import TradeJournal
from app.services.trade_journal import journal_counterfactual_service as svc

# These tests seed/read ``review.investment_watch_events`` on the shared test DB.
# Hold the same advisory lock used by investment-report helper fixtures so a
# concurrent xdist worker cannot TRUNCATE CASCADE the report/watch-event table
# family while the counterfactual sync is selecting or inserting rows.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def cleanup_journal_tables(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeJournalCounterfactual))
    await db_session.execute(delete(TradeJournal))
    await db_session.execute(delete(InvestmentWatchEvent))
    await db_session.commit()


async def _closed_mock_journal(db, *, cid, entry="50000"):
    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal(entry),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid,
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal("10"),
    )
    db.add(j)
    await db.commit()
    return j


@pytest.mark.asyncio
async def test_counterfactual_inserts_and_unique(db_session: AsyncSession):
    cid = f"corr-{uuid.uuid4().hex}"
    j = await _closed_mock_journal(db_session, cid=cid)
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id,
            correlation_id=cid,
            symbol="005930",
            market="kr",
            trigger_price=Decimal("49000"),
            actual_fill_price=Decimal("50000"),
        )
    )
    await db_session.commit()
    # unique correlation_id
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id,
            correlation_id=cid,
            symbol="005930",
            market="kr",
            trigger_price=Decimal("49000"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def _watch_event(db, *, cid, threshold="49000", current_value="49500"):
    ev = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"idem-{uuid.uuid4()}",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=Decimal(threshold),
        threshold_key=str(threshold),
        intent="buy_review",
        action_mode="auto_execute_mock",
        current_value=Decimal(current_value),
        correlation_id=cid,
        kst_date="2026-06-02",
        outcome="executed",
    )
    db.add(ev)
    await db.commit()
    return ev


async def _cfs_for(db, cid):
    return (
        (
            await db.execute(
                select(TradeJournalCounterfactual).where(
                    TradeJournalCounterfactual.correlation_id == cid
                )
            )
        )
        .scalars()
        .all()
    )


def _price_fn(value):
    async def _fn(symbol, market):
        return value

    return _fn


@pytest.mark.asyncio
async def test_sync_records_counterfactual(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid, entry="50000")
    await _watch_event(db_session, cid=cid, threshold="49000", current_value="49500")
    out = await svc.sync_journal_counterfactuals(
        db_session, price_fn=_price_fn(52000.0)
    )
    assert out["created"] == 1
    row = (await _cfs_for(db_session, cid))[0]
    assert row.trigger_price == Decimal("49000")
    assert row.triggered_value == Decimal("49500")
    assert row.actual_fill_price == Decimal("50000")
    assert row.no_action_price == Decimal("52000")
    # (50000-49000)/49000*100 = 2.0408..., (52000-50000)/50000*100 = 4.0
    assert row.fill_vs_trigger_pct == Decimal("2.0408")
    assert row.no_action_vs_fill_pct == Decimal("4.0000")


@pytest.mark.asyncio
async def test_sync_skips_without_watch_event(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(1.0))
    assert out["created"] == 0
    assert await _cfs_for(db_session, cid) == []


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    await _watch_event(db_session, cid=cid)
    await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(52000.0))
    out2 = await svc.sync_journal_counterfactuals(
        db_session, price_fn=_price_fn(52000.0)
    )
    assert out2["created"] == 0
    assert len(await _cfs_for(db_session, cid)) == 1


@pytest.mark.asyncio
async def test_sync_price_fn_none_fail_open(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    cid = f"corr-{uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid, entry="50000")
    await _watch_event(db_session, cid=cid, threshold="49000")
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(None))
    assert out["created"] == 1
    row = (await _cfs_for(db_session, cid))[0]
    assert row.no_action_price is None
    assert row.no_action_vs_fill_pct is None
    assert row.fill_vs_trigger_pct == Decimal("2.0408")


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", False)
    cid = f"corr-{uuid.uuid4().hex}"
    await _closed_mock_journal(db_session, cid=cid)
    await _watch_event(db_session, cid=cid)
    out = await svc.sync_journal_counterfactuals(db_session, price_fn=_price_fn(1.0))
    assert out["status"] == "disabled"
    assert await _cfs_for(db_session, cid) == []
