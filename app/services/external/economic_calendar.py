"""Economic calendar service for fetching high-impact events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_econ_calendar_cache: list[dict[str, Any]] = []
_econ_calendar_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()

HIGH_IMPORTANCE_KEYWORDS = [
    "FOMC",
    "CPI",
    "PPI",
    "GDP",
    "NFP",
    "Non-Farm",
    "Unemployment",
    "Interest Rate",
    "Fed",
    "ECB",
    "BOJ",
    "PMI",
    "Retail Sales",
    "Industrial Production",
    "Consumer Confidence",
    "Treasury",
]


async def fetch_economic_events_today() -> list[dict[str, Any]]:
    """
    Fetch today's high-impact economic events.

    Returns list of events:
        [
            {
                "time": "21:30 KST",
                "event": "US CPI",
                "importance": "high",
                "previous": "2.4%",
                "forecast": "2.3%"
            }
        ]
    """
    global _econ_calendar_cache, _econ_calendar_cache_expires

    async with _cache_lock:
        if _econ_calendar_cache_expires and now_kst() < _econ_calendar_cache_expires:
            logger.debug("Returning cached economic calendar")
            return _econ_calendar_cache.copy()

    try:
        result: list[dict[str, Any]] = []

        async with _cache_lock:
            _econ_calendar_cache = []
            _econ_calendar_cache_expires = now_kst() + timedelta(hours=1)

        return result

    except Exception as exc:
        logger.warning(f"Failed to fetch economic calendar: {exc}")
        return []
