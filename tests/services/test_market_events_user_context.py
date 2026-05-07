"""Unit tests for user_context resolver (ROB-138)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)
from app.models.trading import (
    Exchange,
    Instrument,
    InstrumentType,
    User,
    UserRole,
    UserWatchItem,
)
from app.services.market_events.user_context import (
    UserEventContext,
    get_user_event_context,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(ManualHolding))
    await db_session.execute(delete(BrokerAccount))
    await db_session.execute(delete(UserWatchItem))
    await db_session.execute(delete(Instrument))
    await db_session.execute(delete(Exchange))
    await db_session.execute(delete(User))
    await db_session.commit()
    yield


@pytest.mark.asyncio
@pytest.mark.integration
async def test_returns_empty_sets_for_unknown_user(db_session):
    ctx = await get_user_event_context(db_session, user_id=999)
    assert ctx == UserEventContext(held_tickers=frozenset(), watched_tickers=frozenset())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_collects_manual_holdings_for_user(db_session):
    user = User(id=1, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add(user)
    await db_session.flush()
    acct = BrokerAccount(user_id=1, broker_type=BrokerType.TOSS, account_name="t")
    db_session.add(acct)
    await db_session.flush()
    db_session.add_all([
        ManualHolding(
            broker_account_id=acct.id,
            ticker="AAPL",
            market_type=MarketType.US,
            quantity=1,
            avg_price=1,
        ),
        ManualHolding(
            broker_account_id=acct.id,
            ticker="brk.b",
            market_type=MarketType.US,
            quantity=1,
            avg_price=1,
        ),
    ])
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=1)
    assert ctx.held_tickers == frozenset({"AAPL", "BRK.B"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_collects_watchlist_via_instruments(db_session):
    user = User(id=2, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add(user)
    inst = Instrument(
        symbol="tsla",
        type=InstrumentType.equity_us,
        base_currency="USD",
    )
    db_session.add(inst)
    await db_session.flush()
    db_session.add(UserWatchItem(user_id=2, instrument_id=inst.id, notify_cooldown=timedelta(hours=1)))
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=2)
    assert ctx.watched_tickers == frozenset({"TSLA"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_held_user_filtered_by_user_id(db_session):
    """Held tickers are scoped to the requesting user only."""
    u1 = User(id=10, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    u2 = User(id=11, role=UserRole.viewer, tz="UTC", base_currency="KRW")
    db_session.add_all([u1, u2])
    await db_session.flush()
    acct1 = BrokerAccount(user_id=10, broker_type=BrokerType.TOSS, account_name="a")
    acct2 = BrokerAccount(user_id=11, broker_type=BrokerType.TOSS, account_name="b")
    db_session.add_all([acct1, acct2])
    await db_session.flush()
    db_session.add_all([
        ManualHolding(
            broker_account_id=acct1.id,
            ticker="MSFT",
            market_type=MarketType.US,
            quantity=1,
            avg_price=1,
        ),
        ManualHolding(
            broker_account_id=acct2.id,
            ticker="NVDA",
            market_type=MarketType.US,
            quantity=1,
            avg_price=1,
        ),
    ])
    await db_session.commit()

    ctx10 = await get_user_event_context(db_session, user_id=10)
    assert ctx10.held_tickers == frozenset({"MSFT"})
    ctx11 = await get_user_event_context(db_session, user_id=11)
    assert ctx11.held_tickers == frozenset({"NVDA"})
