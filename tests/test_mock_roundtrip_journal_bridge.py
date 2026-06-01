"""ROB-405 Slice A — mock roundtrip → trade_journal bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger
from app.models.trade_journal import TradeJournal
from app.services.trade_journal.mock_roundtrip_journal_bridge import (
    sync_mock_roundtrip_journals,
)


async def _seed_leg(db, *, cid, side, role, price, lifecycle="reconciled", **over):
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        symbol=over.get("symbol", "005930"),
        instrument_type="equity_kr",
        side=side,
        order_type="limit",
        quantity=Decimal(over.get("quantity", "10")),
        price=Decimal(price),
        amount=Decimal("550000"),
        currency="KRW",
        order_no=f"MOCK-{uuid4()}",
        account_mode="kis_mock",
        broker="kis",
        status="accepted",
        lifecycle_state=lifecycle,
        correlation_id=cid,
        scalping_role=role,
        exit_reason=over.get("exit_reason"),
        thesis=over.get("thesis", "t"),
    )
    db.add(row)
    await db.commit()
    return row


async def _journal_for(db, cid):
    return (
        await db.execute(
            select(TradeJournal).where(TradeJournal.correlation_id == cid)
        )
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_entry_creates_active_journal(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="55000")
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 1
    j = await _journal_for(db_session, cid)
    assert j.status == "active"
    assert j.account_type == "mock"
    assert j.entry_price == Decimal("55000")


@pytest.mark.asyncio
async def test_exit_closes_with_pnl(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    await _seed_leg(
        db_session, cid=cid, side="sell", role="exit", price="55000",
        exit_reason="take_profit",
    )
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 1 and out["closed"] == 1
    j = await _journal_for(db_session, cid)
    assert j.status == "closed"
    assert j.exit_price == Decimal("55000")
    assert j.exit_reason == "take_profit"
    assert j.pnl_pct == Decimal("10.0000")  # (55000-50000)/50000*100


@pytest.mark.asyncio
async def test_idempotent(db_session: AsyncSession):
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    await _seed_leg(db_session, cid=cid, side="sell", role="exit", price="55000")
    await sync_mock_roundtrip_journals(db_session, force=True)
    out2 = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out2["created"] == 0 and out2["closed"] == 0
    rows = (
        await db_session.execute(
            select(TradeJournal).where(TradeJournal.correlation_id == cid)
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ignores_rows_without_correlation_id(db_session: AsyncSession):
    row = KISMockOrderLedger(
        trade_date=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
        symbol="000660", instrument_type="equity_kr", side="buy", order_type="limit",
        quantity=Decimal("1"), price=Decimal("100"), amount=Decimal("100"),
        currency="KRW", order_no=f"MOCK-{uuid4()}", account_mode="kis_mock",
        broker="kis", status="accepted", lifecycle_state="reconciled",
        correlation_id=None, thesis="t",
    )
    db_session.add(row)
    await db_session.commit()
    out = await sync_mock_roundtrip_journals(db_session, force=True)
    assert out["created"] == 0


@pytest.mark.asyncio
async def test_flag_off_disables(db_session: AsyncSession, monkeypatch):
    from app.services.trade_journal import mock_roundtrip_journal_bridge as mod

    monkeypatch.setattr(
        mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", False
    )
    cid = f"corr-{uuid4().hex}"
    await _seed_leg(db_session, cid=cid, side="buy", role="entry", price="50000")
    out = await sync_mock_roundtrip_journals(db_session)  # force defaults False
    assert out["status"] == "disabled"
    assert await _journal_for(db_session, cid) is None
