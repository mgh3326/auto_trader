"""Compute (held / watchlist / both / none) per (market, symbol) for a user."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol

Relation = Literal["held", "watchlist", "both", "none"]
Market = Literal["kr", "us", "crypto"]

# Map InstrumentType values to market strings used by the /invest UI.
_TYPE_TO_MARKET: dict[str, str] = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
    "forex": "us",
    "index": "us",
}


def _norm(symbol: str) -> str:
    try:
        return to_db_symbol(symbol).upper()
    except Exception:
        return symbol.upper()


@dataclass
class RelationResolver:
    held: set[tuple[str, str]] = field(default_factory=set)
    watch: set[tuple[str, str]] = field(default_factory=set)

    def relation(self, market: str, symbol: str) -> Relation:
        key = (market.lower(), _norm(symbol))
        h = key in self.held
        w = key in self.watch
        if h and w:
            return "both"
        if h:
            return "held"
        if w:
            return "watchlist"
        return "none"

    def is_held(self, market: str, symbol: str) -> bool:
        return (market.lower(), _norm(symbol)) in self.held

    def is_watched(self, market: str, symbol: str) -> bool:
        return (market.lower(), _norm(symbol)) in self.watch


async def build_relation_resolver(
    db: AsyncSession,
    *,
    user_id: int,
    held_pairs: list[tuple[str, str]] | None = None,
) -> RelationResolver:
    """Build a resolver for the given user.

    `held_pairs` (market, symbol) can be passed in by callers that already
    have InvestHomeResponse handy. If None, the resolver leaves held empty
    (callers may override) — most callers should pass it in to avoid an
    extra round-trip.
    """
    resolver = RelationResolver()
    if held_pairs:
        resolver.held = {(m.lower(), _norm(s)) for m, s in held_pairs}

    # user_watch_items joins to instruments for symbol/type.
    # If the join fails or the table is unavailable, leave watch empty.
    try:
        from app.models.trading import Instrument, UserWatchItem  # type: ignore
    except ImportError:
        return resolver

    stmt = (
        select(Instrument.symbol, Instrument.type)
        .join(UserWatchItem, UserWatchItem.instrument_id == Instrument.id)
        .where(UserWatchItem.user_id == user_id, UserWatchItem.is_active.is_(True))
    )
    result = await db.execute(stmt)
    for sym, instrument_type in result.all():
        if sym is None or instrument_type is None:
            continue
        market = _TYPE_TO_MARKET.get(str(instrument_type), "us")
        resolver.watch.add((market, _norm(str(sym))))
    return resolver
