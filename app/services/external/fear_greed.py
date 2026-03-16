"""Fear & Greed Index service using alternative.me API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_fear_greed_cache: dict[str, Any] = {}
_fear_greed_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()


async def fetch_fear_greed() -> dict[str, Any] | None:
    """
    Fetch Fear & Greed Index from alternative.me API.

    Returns:
        {
            "value": int,      # 0-100
            "label": str,      # Extreme Fear/Fear/Neutral/Greed/Extreme Greed
            "previous": int,   # Previous day's value
            "trend": str       # improving/stable/deteriorating
        }
        or None if fetch fails
    """
    global _fear_greed_cache, _fear_greed_cache_expires

    async with _cache_lock:
        if _fear_greed_cache_expires and now_kst() < _fear_greed_cache_expires:
            logger.debug("Returning cached Fear & Greed data")
            return _fear_greed_cache.copy()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.alternative.me/fng/", params={"limit": 2}
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(f"Failed to fetch Fear & Greed: {exc}")
        return None

    try:
        values = data.get("data", [])
        if len(values) < 1:
            logger.warning("Fear & Greed API returned empty data")
            return None

        current = values[0]
        previous = values[1] if len(values) > 1 else current

        current_value = int(current.get("value", 0))
        previous_value = int(previous.get("value", 0))

        diff = current_value - previous_value
        if diff > 5:
            trend = "improving"
        elif diff < -5:
            trend = "deteriorating"
        else:
            trend = "stable"
        if current_value <= 20:
            label = "Extreme Fear"
        elif current_value <= 40:
            label = "Fear"
        elif current_value <= 60:
            label = "Neutral"
        elif current_value <= 80:
            label = "Greed"
        else:
            label = "Extreme Greed"

        result = {
            "value": current_value,
            "label": label,
            "previous": previous_value,
            "trend": trend,
        }

        async with _cache_lock:
            _fear_greed_cache = result.copy()
            _fear_greed_cache_expires = now_kst() + timedelta(minutes=30)

        return result

    except Exception as exc:
        logger.warning(f"Failed to parse Fear & Greed response: {exc}")
        return None
