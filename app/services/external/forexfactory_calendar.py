"""ForexFactory economic calendar provider."""

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

FOREXFACTORY_THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FOREXFACTORY_NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"


async def fetch_forexfactory_events_today() -> list[dict[str, Any]]:
    """
    Fetch today's economic events from ForexFactory.

    Returns:
        List of normalized events filtered for today in KST.
    """
    try:
        now = now_kst()
        today_date = now.date()
        is_friday = now.weekday() == 4  # 4 is Friday

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(FOREXFACTORY_THISWEEK_URL)
            response.raise_for_status()
            events = _parse_weekly_events(response.text)

            if is_friday:
                logger.debug("It's Friday KST, also fetching next week's events")
                next_response = await client.get(FOREXFACTORY_NEXTWEEK_URL)
                next_response.raise_for_status()
                next_events = _parse_weekly_events(next_response.text)
                events.extend(next_events)

        # Filter for today
        today_events = [e for e in events if e.get("_kst_date") == today_date]

        # Remove internal _kst_date field before returning
        for e in today_events:
            e.pop("_kst_date", None)

        return today_events

    except Exception as exc:
        logger.warning("Failed to fetch ForexFactory events: %s", exc)
        raise


def _parse_forexfactory_date(date_str: str) -> date | None:
    """Parse date string from ForexFactory XML (e.g., '03-19-2026' or 'Mar 19, 2026')."""
    if not date_str:
        return None

    # Try different formats
    formats = ["%m-%d-%Y", "%b %d, %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    logger.warning("Unknown date format from ForexFactory: %s", date_str)
    return None


def _normalize_value(value: str | None) -> str | None:
    """Convert empty strings to None."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _convert_et_event_to_kst(
    event_date: date,
    time_str: str,
) -> tuple[date, str]:
    if not time_str:
        return event_date, "00:00 KST"

    time_lower = time_str.lower().strip()
    if time_lower in ("", "tentative", "all day"):
        return event_date, "00:00 KST"

    try:
        et_time = datetime.strptime(time_lower, "%I:%M%p")
        total_minutes = et_time.hour * 60 + et_time.minute + (14 * 60)
        day_offset, minute_of_day = divmod(total_minutes, 24 * 60)
        kst_hour, minute = divmod(minute_of_day, 60)
        kst_date = event_date + timedelta(days=day_offset)
        return kst_date, f"{kst_hour:02d}:{minute:02d} KST"
    except ValueError:
        logger.debug("Could not parse time string: %s", time_str)
        return event_date, "00:00 KST"


def _parse_weekly_events(xml_text: str) -> list[dict[str, Any]]:
    """Parse ForexFactory weekly events XML."""
    events = []
    try:
        root = ET.fromstring(xml_text)
        for event_elem in root.findall("event"):
            title = event_elem.findtext("title", "")
            country = event_elem.findtext("country", "")
            date_str = event_elem.findtext("date", "")
            time_str = event_elem.findtext("time", "")
            impact = event_elem.findtext("impact", "")
            forecast = event_elem.findtext("forecast", "")
            previous = event_elem.findtext("previous", "")
            actual = event_elem.findtext("actual", "")

            event_date = _parse_forexfactory_date(date_str)
            if not event_date:
                continue

            kst_date, kst_time = _convert_et_event_to_kst(event_date, time_str)

            events.append({
                "time": kst_time,
                "event": title,
                "country": country,
                "impact": impact.strip().lower(),
                "forecast": _normalize_value(forecast),
                "previous": _normalize_value(previous),
                "actual": _normalize_value(actual),
                "_kst_date": kst_date,  # Internal field for filtering
            })
    except Exception as exc:
        logger.warning("Error parsing ForexFactory XML: %s", exc)

    return events
