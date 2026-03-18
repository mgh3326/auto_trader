"""Economic calendar service for fetching high-impact events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.timezone import now_kst
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    fetch_economic_calendar_finnhub,
)

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


def _is_high_importance_event(event_name: str) -> bool:
    """Check if event matches high-importance keywords."""
    event_upper = event_name.upper()
    return any(keyword.upper() in event_upper for keyword in HIGH_IMPORTANCE_KEYWORDS)


def _convert_time_to_kst(time_str: str) -> str:
    """
    Convert time string to KST format.

    Finnhub returns times in ET (US Eastern Time) during market hours.
    We convert to KST (Korea Standard Time) which is ET + 13 or + 14 hours.

    Args:
        time_str: Time in "HH:MM" format (ET)

    Returns:
        Time in KST format "HH:MM KST"
    """
    if not time_str or ":" not in time_str:
        return "00:00 KST"

    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])

        # KST is ET + 14 hours (simplified, ignoring DST nuances)
        kst_hour = (hour + 14) % 24

        return f"{kst_hour:02d}:{minute:02d} KST"
    except (ValueError, IndexError):
        return "00:00 KST"


def _format_value(value: Any) -> str | None:
    """Format event value (actual/previous/estimate)."""
    if value is None:
        return None
    return str(value).strip()


def _determine_importance(event_name: str, finnhub_impact: str | None) -> str:
    """
    Determine event importance level.

    Priority:
    1. Keyword matching for known high-impact events
    2. Finnhub impact field if available
    3. Default to medium
    """
    if _is_high_importance_event(event_name):
        return "high"

    if finnhub_impact:
        impact_lower = finnhub_impact.lower()
        if impact_lower in ("high", "medium", "low"):
            return impact_lower

    return "medium"


async def fetch_economic_events_today() -> list[dict[str, Any]]:
    """
    Fetch today's high-impact economic events.

    Returns list of events in N8nEconomicEvent format:
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
            logger.debug("Returning cached economic calendar (%d events)", len(_econ_calendar_cache))
            return _econ_calendar_cache.copy()

    try:
        today = now_kst().strftime("%Y-%m-%d")
        logger.info("Fetching economic calendar for date=%s (KST)", today)

        events = await fetch_economic_calendar_finnhub(today, today)

        if events is None:
            logger.warning(
                "Finnhub economic calendar returned None for date=%s — "
                "check FINNHUB_API_KEY and API connectivity",
                today,
            )
            async with _cache_lock:
                _econ_calendar_cache = []
                _econ_calendar_cache_expires = now_kst() + timedelta(minutes=15)
            return []

        if not events:
            logger.info(
                "Finnhub returned 0 events for date=%s — no US events scheduled",
                today,
            )

        transformed_events: list[dict[str, Any]] = []
        for event in events:
            event_name = str(event.get("event", "")).strip()
            if not event_name:
                continue

            importance = _determine_importance(event_name, event.get("impact"))

            transformed_event = {
                "time": _convert_time_to_kst(str(event.get("time", "")).strip()),
                "event": event_name,
                "importance": importance,
                "previous": _format_value(event.get("previous")),
                "forecast": _format_value(event.get("estimate")),
            }
            transformed_events.append(transformed_event)

        transformed_events.sort(key=lambda x: x["time"])

        async with _cache_lock:
            _econ_calendar_cache = transformed_events.copy()
            _econ_calendar_cache_expires = now_kst() + timedelta(hours=1)

        logger.info("Fetched %d economic events for today", len(transformed_events))
        return transformed_events

    except Exception as exc:
        logger.warning("Failed to fetch economic calendar: %s", exc)
        async with _cache_lock:
            _econ_calendar_cache = []
            _econ_calendar_cache_expires = now_kst() + timedelta(minutes=15)
        return []


def _clear_economic_calendar_cache() -> None:
    """Clear economic calendar cache (for testing/debugging)."""
    global _econ_calendar_cache, _econ_calendar_cache_expires
    _econ_calendar_cache = []
    _econ_calendar_cache_expires = None
