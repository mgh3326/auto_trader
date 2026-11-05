"""Canonical market detection and normalization helpers for MCP tools.

All market-related detection and normalization functions are gathered here.
``app.mcp_server.tooling.shared`` continues to re-export the core subset
(``is_korean_equity_code``, ``is_crypto_market``, ``is_us_equity_symbol``,
``normalize_market``, ``resolve_market_type``) for backward compatibility.
"""

from __future__ import annotations

from app.core.market import (
    CRYPTO_MARKET_ALIASES,
    KR_MARKET_ALIASES,
    US_MARKET_ALIASES,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market,
    is_korean_equity_code,
    is_us_equity_symbol,
    normalize_market,
    resolve_market_type,
)

# Alias table lives in app.core.market so services and MCP tools agree.
_KR_ALIASES = KR_MARKET_ALIASES
_US_ALIASES = US_MARKET_ALIASES
_CRYPTO_ALIASES = CRYPTO_MARKET_ALIASES


def normalize_equity_market(market: str) -> str:
    """Normalize a market string to 'kr' or 'us'. Raises ValueError for crypto/unknown."""
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

    Returns 'kr' or 'us'. Raises ValueError for crypto symbols or unknown market.
    """
    if market is not None:
        return normalize_equity_market(market)
    if is_crypto_market(symbol):
        raise ValueError("not available for cryptocurrencies")
    if is_korean_equity_code(symbol):
        return "kr"
    return "us"


__all__ = [
    "detect_equity_market",
    "is_crypto_market",
    "is_korean_equity_code",
    "is_us_equity_symbol",
    "normalize_equity_market",
    "normalize_market",
    "normalize_market_with_crypto",
    "resolve_market_type",
]
