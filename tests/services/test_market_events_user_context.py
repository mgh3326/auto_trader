"""Unit tests for user_context resolver (ROB-138)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)
from app.models.trading import (
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


@pytest_asyncio.fixture
async def make_user(db_session):
    async def _make_user() -> User:
        suffix = uuid4().hex
        user = User(
            id=1_000_000_000 + int(suffix[:8], 16),
            email=f"market-events-{suffix}@example.com",
            username=f"market_events_{suffix}",
            role=UserRole.viewer,
            tz="UTC",
            base_currency="KRW",
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make_user


@pytest.mark.asyncio
@pytest.mark.integration
async def test_returns_empty_sets_for_unknown_user(db_session):
    ctx = await get_user_event_context(db_session, user_id=999)
    assert ctx == UserEventContext(
        held_tickers=frozenset(), watched_tickers=frozenset()
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_collects_manual_holdings_for_user(db_session, make_user):
    user = await make_user()
    acct = BrokerAccount(user_id=user.id, broker_type=BrokerType.TOSS, account_name="t")
    db_session.add(acct)
    await db_session.flush()
    db_session.add_all(
        [
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
        ]
    )
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=user.id)
    assert ctx.held_tickers == frozenset({"AAPL", "BRK.B"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_collects_watchlist_via_instruments(db_session, make_user):
    user = await make_user()
    symbol = f"TCTX{uuid4().hex[:8]}"
    inst = Instrument(
        symbol=symbol,
        type=InstrumentType.equity_us,
        base_currency="USD",
    )
    db_session.add(inst)
    await db_session.flush()
    db_session.add(
        UserWatchItem(
            user_id=user.id, instrument_id=inst.id, notify_cooldown=timedelta(hours=1)
        )
    )
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=user.id)
    assert ctx.watched_tickers == frozenset({symbol.upper()})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_held_user_filtered_by_user_id(db_session, make_user):
    """Held tickers are scoped to the requesting user only."""
    u1 = await make_user()
    u2 = await make_user()
    acct1 = BrokerAccount(user_id=u1.id, broker_type=BrokerType.TOSS, account_name="a")
    acct2 = BrokerAccount(user_id=u2.id, broker_type=BrokerType.TOSS, account_name="b")
    db_session.add_all([acct1, acct2])
    await db_session.flush()
    db_session.add_all(
        [
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
        ]
    )
    await db_session.commit()

    ctx10 = await get_user_event_context(db_session, user_id=u1.id)
    assert ctx10.held_tickers == frozenset({"MSFT"})
    ctx11 = await get_user_event_context(db_session, user_id=u2.id)
    assert ctx11.held_tickers == frozenset({"NVDA"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ignores_inactive_accounts_and_zero_quantity_holdings(
    db_session, make_user
):
    user = await make_user()
    active_acct = BrokerAccount(
        user_id=user.id, broker_type=BrokerType.TOSS, account_name="active"
    )
    inactive_acct = BrokerAccount(
        user_id=user.id,
        broker_type=BrokerType.TOSS,
        account_name="inactive",
        is_active=False,
    )
    db_session.add_all([active_acct, inactive_acct])
    await db_session.flush()
    db_session.add_all(
        [
            ManualHolding(
                broker_account_id=active_acct.id,
                ticker="LIVE",
                market_type=MarketType.US,
                quantity=1,
                avg_price=1,
            ),
            ManualHolding(
                broker_account_id=active_acct.id,
                ticker="SOLD",
                market_type=MarketType.US,
                quantity=0,
                avg_price=1,
            ),
            ManualHolding(
                broker_account_id=inactive_acct.id,
                ticker="OLD",
                market_type=MarketType.US,
                quantity=1,
                avg_price=1,
            ),
        ]
    )
    await db_session.commit()

    ctx = await get_user_event_context(db_session, user_id=user.id)
    assert ctx.held_tickers == frozenset({"LIVE"})
