"""Per-day ForexFactory economic-calendar fetch helper (ROB-132).

Fetches `ff_calendar_thisweek.xml` and (when needed) `ff_calendar_nextweek.xml`,
parses each event into a uniform dict, converts ET wall-clock times to UTC, and
filters to rows whose ET-day matches the requested target_date.

Caller passes the resulting rows into
`app.services.market_events.normalizers.normalize_forexfactory_event_row`.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"
ET_TZ = ZoneInfo("America/New_York")


async def _fetch_xml_documents(target_date: date) -> list[str]:
    """Fetch this-week, plus next-week if target_date is within 7 days of next Monday.

    Kept as a module-level seam for tests to patch.
    """
    urls = [THISWEEK_URL]
    today = datetime.now(UTC).date()
    if (target_date - today).days >= 5:
        urls.append(NEXTWEEK_URL)
    documents: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in urls:
            response = await client.get(url)
            response.raise_for_status()
            documents.append(response.text)
    return documents


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%m-%d-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_et_time(time_str: str | None) -> tuple[int, int] | None:
    if not time_str:
        return None
    cleaned = time_str.strip().lower()
    if cleaned in ("", "all day", "tentative"):
        return None
    try:
        parsed = datetime.strptime(cleaned, "%I:%M%p")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


def _parse_one_xml(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("forexfactory XML parse error: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for elem in root.findall("event"):
        title = (elem.findtext("title") or "").strip()
        currency = (elem.findtext("country") or "").strip()
        date_str = elem.findtext("date") or ""
        time_str = elem.findtext("time") or ""
        impact = (elem.findtext("impact") or "").strip().lower()
        forecast = elem.findtext("forecast") or ""
        previous = elem.findtext("previous") or ""
        actual = elem.findtext("actual") or ""

        event_date = _parse_date(date_str)
        if event_date is None:
            continue

        hm = _parse_et_time(time_str)
        if hm is None:
            release_local = None
            release_utc = None
            id_iso = event_date.isoformat()
        else:
            hour, minute = hm
            release_local = datetime(
                event_date.year,
                event_date.month,
                event_date.day,
                hour,
                minute,
            )
            release_utc = release_local.replace(tzinfo=ET_TZ).astimezone(UTC)
            id_iso = release_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        source_event_id = f"ff::{currency}::{title}::{id_iso}"

        rows.append(
            {
                "title": title,
                "currency": currency or None,
                "country": currency or None,
                "event_date": event_date,
                "release_time_utc": release_utc,
                "release_time_local": release_local,
                "time_hint_raw": time_str,
                "impact": impact,
                "actual": actual or None,
                "forecast": forecast or None,
                "previous": previous or None,
                "source_event_id": source_event_id,
            }
        )
    return rows


async def fetch_forexfactory_events_for_date(
    target_date: date,
) -> list[dict[str, Any]]:
    """Return ForexFactory rows whose ET-day == target_date.

    Returns [] on XML parse error so the caller can decide whether to mark the
    partition failed (typically only network errors should cause failure).
    """
    try:
        documents = await _fetch_xml_documents(target_date)
    except Exception as exc:
        logger.warning("forexfactory fetch failed for %s: %s", target_date, exc)
        raise

    rows: list[dict[str, Any]] = []
    for xml_text in documents:
        rows.extend(_parse_one_xml(xml_text))

    return [r for r in rows if r["event_date"] == target_date]
