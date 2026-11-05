"""Canonical market alias table.

One place decides which caller spellings mean KR equity, US equity or crypto,
so ``krx`` / ``naver`` / ``usdt`` etc. resolve the same way in every tool and
service that accepts a free-form ``market`` argument. Callers keep their own
output vocabulary (``kr`` vs ``equity_kr``) and their own unknown-value policy
(raise vs ``None``); only the alias -> market mapping lives here.
"""

from __future__ import annotations

from typing import Literal

MarketKey = Literal["kr", "us", "crypto"]

KR_MARKET_ALIASES: frozenset[str] = frozenset(
    {"kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver"}
)
US_MARKET_ALIASES: frozenset[str] = frozenset(
    {"us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"}
)
CRYPTO_MARKET_ALIASES: frozenset[str] = frozenset({"crypto", "upbit", "krw", "usdt"})

_INSTRUMENT_TYPE: dict[MarketKey, str] = {
    "kr": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}


def market_key(market: str | None) -> MarketKey | None:
    """Return ``kr`` / ``us`` / ``crypto`` for a known alias, else ``None``."""
    if market is None:
        return None
    normalized = str(market).strip().lower()
    if normalized in KR_MARKET_ALIASES:
        return "kr"
    if normalized in US_MARKET_ALIASES:
        return "us"
    if normalized in CRYPTO_MARKET_ALIASES:
        return "crypto"
    return None


def market_instrument_type(market: str | None) -> str | None:
    """Return ``equity_kr`` / ``equity_us`` / ``crypto`` for a known alias."""
    key = market_key(market)
    return _INSTRUMENT_TYPE[key] if key is not None else None


__all__ = [
    "CRYPTO_MARKET_ALIASES",
    "KR_MARKET_ALIASES",
    "MarketKey",
    "US_MARKET_ALIASES",
    "market_instrument_type",
    "market_key",
]
