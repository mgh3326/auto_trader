"""ROB-405 Slice B — trade_journal verdict."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal
from app.services.trade_journal import journal_verdict_service as svc


@pytest_asyncio.fixture(autouse=True)
async def cleanup_journal_tables(db_session: AsyncSession):
    await db_session.execute(delete(TradeJournalReview))
    await db_session.execute(delete(TradeJournal))
    await db_session.commit()


async def _closed_mock_journal(db, *, pnl_pct, cid=None):

    j = TradeJournal(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        entry_price=Decimal("50000"),
        quantity=Decimal("10"),
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=cid or f"corr-{uuid4().hex}",
        status="closed",
        exit_price=Decimal("55000"),
        exit_date=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_pct=Decimal(pnl_pct),
    )
    db.add(j)
    await db.flush()
    return j


@pytest.mark.asyncio
async def test_journal_review_inserts_and_checks(db_session: AsyncSession):
    j = await _closed_mock_journal(db_session, pnl_pct="10")
    r = TradeJournalReview(
        journal_id=j.id, verdict="good", verdict_source="auto", pnl_pct=Decimal("10")
    )
    db_session.add(r)
    await db_session.commit()
    assert r.id is not None


@pytest.mark.asyncio
async def test_journal_review_verdict_check_rejects(db_session: AsyncSession):
    j = await _closed_mock_journal(db_session, pnl_pct="1")
    db_session.add(
        TradeJournalReview(journal_id=j.id, verdict="great", verdict_source="auto")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def _reviews_for(db, journal_id):
    return (
        (
            await db.execute(
                select(TradeJournalReview).where(
                    TradeJournalReview.journal_id == journal_id
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_sync_records_auto_verdict(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    j = await _closed_mock_journal(db_session, pnl_pct="2.0")
    out = await svc.sync_journal_verdicts(db_session)
    assert out["created"] == 1
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict == "good"
    assert rows[0].verdict_source == "auto"


@pytest.mark.asyncio
async def test_sync_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    j = await _closed_mock_journal(db_session, pnl_pct="-2.0")
    await svc.sync_journal_verdicts(db_session)
    out2 = await svc.sync_journal_verdicts(db_session)
    assert out2["created"] == 0
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict == "bad"


@pytest.mark.asyncio
async def test_sync_ignores_non_closed_and_non_mock(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", True)
    # active mock journal
    j_active = TradeJournal(
        symbol="A",
        instrument_type="equity_kr",
        side="buy",
        thesis="t",
        account_type="mock",
        account="kis_mock",
        correlation_id=f"c-{uuid4().hex}",
        status="active",
    )
    # closed live journal
    j_live = TradeJournal(
        symbol="B",
        instrument_type="equity_kr",
        side="buy",
        thesis="t",
        account_type="live",
        status="closed",
        pnl_pct=Decimal("5"),
    )
    db_session.add_all([j_active, j_live])
    await db_session.commit()
    out = await svc.sync_journal_verdicts(db_session)
    assert out["created"] == 0


@pytest.mark.asyncio
async def test_flag_off_disables(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "JOURNAL_VERDICT_AUTO_ENABLED", False)
    j = await _closed_mock_journal(db_session, pnl_pct="2.0")
    out = await svc.sync_journal_verdicts(db_session)
    assert out["status"] == "disabled"
    assert await _reviews_for(db_session, j.id) == []


@pytest.mark.asyncio
async def test_record_manual_verdict(db_session):
    j = await _closed_mock_journal(db_session, pnl_pct="0.0")
    out = await svc.record_manual_verdict(
        db_session, journal_id=j.id, verdict="bad", comment="thesis broke"
    )
    assert out["status"] == "ok"
    rows = await _reviews_for(db_session, j.id)
    assert len(rows) == 1
    assert rows[0].verdict_source == "manual"
    with pytest.raises(ValueError):
        await svc.record_manual_verdict(db_session, journal_id=j.id, verdict="great")
