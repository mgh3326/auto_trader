"""Economic calendar service for fetching high-impact events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.timezone import now_kst
from app.services.external.forexfactory_calendar import (
    fetch_forexfactory_events_today,
)

logger = logging.getLogger(__name__)

_econ_calendar_cache: list[dict[str, Any]] = []
_econ_calendar_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()


async def fetch_economic_events_today() -> list[dict[str, Any]]:
    """
    Fetch today's high-impact economic events from ForexFactory.

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
            logger.debug(
                "Returning cached economic calendar (%d events)",
                len(_econ_calendar_cache),
            )
            return _econ_calendar_cache.copy()

    try:
        logger.info("Fetching economic calendar from ForexFactory")

        raw_events = await fetch_forexfactory_events_today()

        transformed_events: list[dict[str, Any]] = []
        for event in raw_events:
            transformed_event = {
                "time": event["time"],
                "event": event["event"],
                "importance": event.get("impact", "medium"),
                "previous": event.get("previous"),
                "forecast": event.get("forecast"),
            }
            transformed_events.append(transformed_event)

        transformed_events.sort(key=lambda x: x["time"])

        async with _cache_lock:
            _econ_calendar_cache = transformed_events.copy()
            _econ_calendar_cache_expires = now_kst() + timedelta(hours=1)

        logger.info(
            "Fetched %d economic events from ForexFactory", len(transformed_events)
        )
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
