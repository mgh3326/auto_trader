"""Shared helpers for fundamentals tool handlers.

All market normalization logic now lives in
``app.mcp_server.tooling.market_normalization``. This module re-exports
for backward compatibility with ``_financials.py``, ``_news.py``,
``_profiles.py``, ``_valuation.py`` that import from here.
"""

from __future__ import annotations

from app.mcp_server.tooling.market_normalization import (
    detect_equity_market,
    normalize_equity_market,
    normalize_market_with_crypto,
)

__all__ = [
    "detect_equity_market",
    "normalize_equity_market",
    "normalize_market_with_crypto",
]
