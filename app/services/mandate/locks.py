"""Lock order of the mandate model (hk 3795 §4.6, ticket T1 "lock 순서 정의").

Caller-side transaction locks, always taken in this order and never the other way:

    L0  proposal dispatch   (existing, ``order_proposals:auto_dispatch:<id>``)
    L1  symbol              ``mandate_symbol:<realm>:<market>:<symbol>``
    L2  basket              ``mandate_basket:<realm>:<basket_key>``
    L3  sleeve              ``review.strategy_sleeves:<id>`` (existing)
    L4a/L4b                 auto-approval day locks (existing / T5)

The fence-area locks F1..F4 are a separate set, owned by T4c/T5, and are not
defined here. A lock is a serialization aid inside one transaction, never the
authority to send: authority is a committed row (§4.6 "fence가 권한이다").

The symbol key string is also built inside the reservation trigger
(``'mandate_symbol:'||realm||':'||market||':'||symbol``); a test pins the two.
"""

from __future__ import annotations

from enum import IntEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mandate import vocabulary as v
from app.services.strategy_sleeves.store import acquire_sleeve_lock


class LockLevel(IntEnum):
    SYMBOL = 1
    BASKET = 2
    SLEEVE = 3


#: The only legal acquisition order within the mandate model.
LOCK_ORDER: tuple[LockLevel, ...] = (
    LockLevel.SYMBOL,
    LockLevel.BASKET,
    LockLevel.SLEEVE,
)


def symbol_lock_key(realm: str, market: str, symbol: str) -> str:
    if realm not in v.REALMS:
        raise ValueError(f"unknown realm {realm!r}")
    return f"mandate_symbol:{realm}:{market}:{symbol}"


def basket_lock_key(realm: str, basket_key: str) -> str:
    if realm not in v.REALMS:
        raise ValueError(f"unknown realm {realm!r}")
    return f"mandate_basket:{realm}:{basket_key}"


async def _advisory_xact(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def acquire_symbol_lock(
    session: AsyncSession, realm: str, market: str, symbol: str
) -> None:
    """L1: serialize plan registration and release of one instrument in one realm."""
    await _advisory_xact(session, symbol_lock_key(realm, market, symbol))


async def acquire_basket_lock(
    session: AsyncSession, realm: str, basket_key: str
) -> None:
    """L2: serialize budget, sector and approval decisions of one basket."""
    await _advisory_xact(session, basket_lock_key(realm, basket_key))


async def acquire_in_order(
    session: AsyncSession,
    *,
    realm: str,
    market: str | None = None,
    symbol: str | None = None,
    basket_key: str | None = None,
    sleeve_id: int | None = None,
) -> None:
    """Take whichever of L1, L2, L3 are asked for, in ``LOCK_ORDER``."""
    if symbol is not None:
        if market is None:
            raise ValueError("a symbol lock needs a market")
        await acquire_symbol_lock(session, realm, market, symbol)
    if basket_key is not None:
        await acquire_basket_lock(session, realm, basket_key)
    if sleeve_id is not None:
        await acquire_sleeve_lock(session, sleeve_id)
