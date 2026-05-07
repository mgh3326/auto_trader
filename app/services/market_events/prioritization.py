"""Market event prioritization logic for Discover calendar (ROB-138).

Priority tiers (high → low):
  HELD              : event symbol in user holdings
  WATCHED           : event symbol in user watchlist
  MAJOR             : event symbol in market-specific allowlist (top liquid names)
  HIGH_IMPORTANCE   : economic / disclosure with importance >= 3
  MEDIUM_IMPORTANCE : economic / disclosure with importance == 2
  OTHER             : everything else
"""

from __future__ import annotations

from enum import IntEnum

from app.schemas.market_events import MarketEventResponse
from app.services.market_events.user_context import UserEventContext


class Priority(IntEnum):
    HELD = 0
    WATCHED = 1
    MAJOR = 2
    HIGH_IMPORTANCE = 3
    MEDIUM_IMPORTANCE = 4
    OTHER = 5


MAJOR_TICKERS: dict[str, frozenset[str]] = {
    "us": frozenset(
        {
            "AAPL",
            "MSFT",
            "GOOGL",
            "GOOG",
            "AMZN",
            "NVDA",
            "META",
            "TSLA",
            "AVGO",
            "BRK.B",
            "LLY",
            "JPM",
            "V",
            "UNH",
            "XOM",
            "MA",
            "WMT",
            "JNJ",
            "PG",
            "ORCL",
            "HD",
            "BAC",
            "ABBV",
            "KO",
            "PEP",
            "CVX",
            "MRK",
            "COST",
            "AMD",
            "NFLX",
            "ADBE",
            "CRM",
            "DIS",
            "PFE",
        }
    ),
    "kr": frozenset(
        {
            "005930",
            "000660",
            "035420",
            "207940",
            "005380",
            "035720",
            "051910",
            "006400",
            "000270",
            "068270",
            "105560",
        }
    ),
    "crypto": frozenset({"BTC", "ETH", "SOL", "XRP", "BNB"}),
    "global": frozenset(),
}


def _norm(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    return symbol.strip().upper() or None


def compute_priority(event: MarketEventResponse, ctx: UserEventContext) -> Priority:
    sym = _norm(event.symbol)
    if sym is not None:
        if sym in ctx.held_tickers:
            return Priority.HELD
        if sym in ctx.watched_tickers:
            return Priority.WATCHED
        major = MAJOR_TICKERS.get(event.market, frozenset())
        if sym in major:
            return Priority.MAJOR

    importance = event.importance or 0
    if importance >= 3:
        return Priority.HIGH_IMPORTANCE
    if importance == 2:
        return Priority.MEDIUM_IMPORTANCE
    return Priority.OTHER
