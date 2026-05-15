"""Upbit public recent-trades fetcher (read-only)."""

from __future__ import annotations

from typing import Any

from app.services.brokers.upbit.client import UPBIT_REST, _request_json
from app.services.upbit_symbol_universe_service import get_upbit_market_by_coin

UPBIT_TRADES_URL = f"{UPBIT_REST}/trades/ticks"
_MAX_COUNT = 500


async def _normalize_market(market: str) -> str:
    code = str(market or "").strip().upper()
    if not code:
        raise ValueError("market is required")
    if code.startswith("KRW-"):
        return code
    resolved = await get_upbit_market_by_coin(code)
    if not resolved:
        raise ValueError(f"unknown Upbit market: {market!r}")
    return resolved


async def fetch_recent_trades(
    market: str = "KRW-BTC", count: int = 50
) -> list[dict[str, Any]]:
    """Return recent public trades for a KRW Upbit market.

    This is read-only and only calls Upbit's public /v1/trades/ticks endpoint.
    Transport/status errors are deliberately propagated so callers can classify
    them into freshness/error envelopes.
    """
    normalized = await _normalize_market(market)
    bounded = max(1, min(int(count), _MAX_COUNT))
    rows = await _request_json(
        UPBIT_TRADES_URL, params={"market": normalized, "count": bounded}
    )
    if not isinstance(rows, list):
        raise ValueError("unexpected Upbit trades response shape")
    return list(rows)
