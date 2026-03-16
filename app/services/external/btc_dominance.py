"""BTC dominance service using CoinGecko API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_btc_dominance_cache: dict[str, Any] = {}
_btc_dominance_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()

COIN_GECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


def _clear_btc_dominance_cache() -> None:
    """Clear in-memory BTC dominance cache (for tests)."""
    global _btc_dominance_cache, _btc_dominance_cache_expires
    _btc_dominance_cache = {}
    _btc_dominance_cache_expires = None


async def fetch_btc_dominance() -> dict[str, Any] | None:
    """
    Fetch BTC dominance and global market data from CoinGecko.

    Returns:
        {
            "btc_dominance": float,  # BTC market cap percentage
            "total_market_cap_change_24h": float  # 24h change %
        }
        or None if fetch fails
    """
    global _btc_dominance_cache, _btc_dominance_cache_expires

    async with _cache_lock:
        if _btc_dominance_cache_expires and now_kst() < _btc_dominance_cache_expires:
            logger.debug("Returning cached BTC dominance data")
            return _btc_dominance_cache.copy()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(COIN_GECKO_GLOBAL_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(f"Failed to fetch BTC dominance: {exc}")
        async with _cache_lock:
            _btc_dominance_cache = {}
            _btc_dominance_cache_expires = None
        return None

    try:
        market_data = data.get("data", {})
        market_cap_pct = market_data.get("market_cap_percentage", {})
        btc_dominance = market_cap_pct.get("btc")
        market_cap_change = market_data.get(
            "market_cap_change_percentage_24h_usd",
        )

        if btc_dominance is None:
            logger.warning("BTC dominance not found in CoinGecko response")
            return None

        result = {
            "btc_dominance": round(float(btc_dominance), 2),
            "total_market_cap_change_24h": (
                round(float(market_cap_change), 2)
                if market_cap_change is not None
                else None
            ),
        }

        async with _cache_lock:
            _btc_dominance_cache = result.copy()
            _btc_dominance_cache_expires = now_kst() + timedelta(minutes=30)

        return result

    except Exception as exc:
        logger.warning(f"Failed to parse BTC dominance response: {exc}")
        return None
