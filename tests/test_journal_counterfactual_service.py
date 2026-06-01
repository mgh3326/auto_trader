"""ROB-405 Slice C — journal counterfactual."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeJournalCounterfactual
from app.models.trade_journal import TradeJournal


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
    cid = f"corr-{uuid4().hex}"
    j = await _closed_mock_journal(db_session, cid=cid)
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market="kr",
            trigger_price=Decimal("49000"), actual_fill_price=Decimal("50000"),
        )
    )
    await db_session.commit()
    # unique correlation_id
    db_session.add(
        TradeJournalCounterfactual(
            journal_id=j.id, correlation_id=cid, symbol="005930", market="kr",
            trigger_price=Decimal("49000"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
