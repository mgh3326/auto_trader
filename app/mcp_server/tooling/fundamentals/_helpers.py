"""Shared helpers for fundamentals tool handlers."""

from __future__ import annotations

from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)

# Alias sets for market string normalization
_KR_ALIASES = frozenset(
    {"kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver"}
)
_US_ALIASES = frozenset({"us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"})
_CRYPTO_ALIASES = frozenset({"crypto", "upbit", "krw", "usdt"})


def normalize_equity_market(market: str) -> str:
    """Normalize a market string to 'kr' or 'us'. Raises ValueError otherwise."""
    m = market.strip().lower()
    if m in _KR_ALIASES:
        return "kr"
    if m in _US_ALIASES:
        return "us"
    raise ValueError("market must be 'us' or 'kr'")


def normalize_market_with_crypto(market: str) -> str:
    """Normalize a market string to 'kr', 'us', or 'crypto'. Raises ValueError otherwise."""
    m = market.strip().lower()
    if m in _CRYPTO_ALIASES:
        return "crypto"
    if m in _KR_ALIASES:
        return "kr"
    if m in _US_ALIASES:
        return "us"
    raise ValueError("market must be 'us', 'kr', or 'crypto'")


def detect_equity_market(symbol: str, market: str | None) -> str:
    """Auto-detect equity market from symbol, or normalize explicit market.

    Returns 'kr' or 'us'. Raises ValueError for crypto symbols.
    """
    if market is not None:
        return normalize_equity_market(market)
    if _is_crypto_market(symbol):
        raise ValueError("not available for cryptocurrencies")
    if _is_korean_equity_code(symbol):
        return "kr"
    return "us"
