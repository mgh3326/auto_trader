"""ROB-405 Slice B — trade_journal verdict."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeJournalReview
from app.models.trade_journal import TradeJournal


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
    await db.commit()
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
