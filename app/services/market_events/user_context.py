"""Per-user holdings/watchlist resolver for market event prioritization (ROB-138)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import BrokerAccount, ManualHolding
from app.models.trading import Instrument, UserWatchItem


@dataclass(frozen=True)
class UserEventContext:
    held_tickers: frozenset[str]
    watched_tickers: frozenset[str]


async def get_user_event_context(
    db: AsyncSession, *, user_id: int
) -> UserEventContext:
    held_stmt = (
        select(ManualHolding.ticker)
        .join(BrokerAccount, BrokerAccount.id == ManualHolding.broker_account_id)
        .where(BrokerAccount.user_id == user_id)
    )
    held = {t.upper() for (t,) in (await db.execute(held_stmt)).all() if t}

    watched_stmt = (
        select(Instrument.symbol)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
    )
    watched = {s.upper() for (s,) in (await db.execute(watched_stmt)).all() if s}

    return UserEventContext(
        held_tickers=frozenset(held),
        watched_tickers=frozenset(watched),
    )
