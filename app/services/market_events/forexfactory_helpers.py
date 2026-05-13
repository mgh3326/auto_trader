"""Per-day ForexFactory economic-calendar fetch helper (ROB-132).

Fetches `ff_calendar_thisweek.xml` and (when needed) `ff_calendar_nextweek.xml`,
parses each event into a uniform dict, treats ForexFactory feed times as UTC,
and filters to rows whose feed date matches the requested target_date.

Caller passes the resulting rows into
`app.services.market_events.normalizers.normalize_forexfactory_event_row`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"
ET_TZ = ZoneInfo("America/New_York")
FOREXFACTORY_FEED_TZ = UTC

RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ForexFactoryFetchError(Exception):
    def __init__(self, reason: str, *, cause: Exception | None = None):
        super().__init__(f"forexfactory_fetch_error:{reason}")
        self.reason = reason
        self.__cause__ = cause


async def _sleep_with_jitter(seconds: float) -> None:
    jittered = seconds * (1 + random.uniform(-0.25, 0.25))
    await asyncio.sleep(max(jittered, 0))


async def _fetch_one_xml(
    url: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> str:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code in RETRIABLE_STATUS:
                    retry_after_hdr = response.headers.get("Retry-After")
                    retry_after = float(retry_after_hdr) if retry_after_hdr else None
                    delay = (
                        retry_after
                        if retry_after is not None
                        else min(base_delay * (2**attempt), 30.0)
                    )
                    if attempt < max_attempts - 1:
                        await _sleep_with_jitter(delay)
                        continue
                    reason = (
                        "rate_limited"
                        if response.status_code == 429
                        else "upstream_5xx"
                    )
                    raise ForexFactoryFetchError(reason)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in RETRIABLE_STATUS and attempt < max_attempts - 1:
                last_exc = exc
                await _sleep_with_jitter(min(base_delay * (2**attempt), 30.0))
                continue
            if status == 429:
                raise ForexFactoryFetchError("rate_limited", cause=exc) from exc
            if 500 <= status < 600:
                raise ForexFactoryFetchError("upstream_5xx", cause=exc) from exc
            raise ForexFactoryFetchError("upstream_4xx", cause=exc) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await _sleep_with_jitter(min(base_delay * (2**attempt), 30.0))
                continue
            raise ForexFactoryFetchError("network_error", cause=exc) from exc
    raise ForexFactoryFetchError("unknown", cause=last_exc)


def rolling_window_for_today(now_utc: datetime) -> tuple[date, date]:
    """Return (start, end) inclusive of the rolling FF window in ET dates.

    Upstream publishes ISO-week thisweek + nextweek feeds anchored Monday
    in ET. The returned dates are ET-local calendar dates.
    """
    now_et = now_utc.astimezone(ET_TZ)
    today_et = now_et.date()
    # Python: Mon=0..Sun=6. Snap back to Monday.
    monday_this = today_et - timedelta(days=today_et.weekday())
    sunday_next = monday_this + timedelta(days=13)
    return monday_this, sunday_next


class ForexFactoryWeeklyCache:
    """Lazily fetches thisweek/nextweek XML at most once per run."""

    def __init__(self, *, now_utc: datetime | None = None) -> None:
        self._now_utc = now_utc or datetime.now(UTC)
        self._window_start, self._window_end = rolling_window_for_today(self._now_utc)
        # thisweek covers [window_start, window_start + 6 days].
        self._thisweek_end = self._window_start + timedelta(days=6)
        self._payloads: dict[str, list[dict[str, Any]]] = {}

    def _url_for(self, target_date: date) -> str | None:
        if target_date < self._window_start or target_date > self._window_end:
            return None
        if target_date <= self._thisweek_end:
            return THISWEEK_URL
        return NEXTWEEK_URL

    def _candidate_urls_for(self, target_date: date) -> list[str] | None:
        """Return URL candidates ordered by safest content-based selection.

        ForexFactory can roll `thisweek` ahead before ET Monday while `nextweek`
        is still unavailable. Fetch `thisweek` first and trust the parsed payload's
        event week before falling back to `nextweek`, instead of routing solely by
        local ET week math.
        """
        if target_date < self._window_start or target_date > self._window_end:
            return None
        return [THISWEEK_URL, NEXTWEEK_URL]

    @staticmethod
    def _payload_covers_date(rows: list[dict[str, Any]], target_date: date) -> bool:
        """Infer whether a parsed weekly payload is the upstream week for a date."""
        event_dates = [r["event_date"] for r in rows if r.get("event_date")]
        if not event_dates:
            return False
        week_start = min(event_dates) - timedelta(days=min(event_dates).weekday())
        week_end = week_start + timedelta(days=6)
        return week_start <= target_date <= week_end

    async def _ensure_payload(self, url: str) -> list[dict[str, Any]]:
        cached = self._payloads.get(url)
        if cached is not None:
            return cached
        xml_text = await _fetch_one_xml(url)
        parsed = _parse_one_xml(xml_text)
        self._payloads[url] = parsed
        return parsed

    async def get_events_for_date(
        self, target_date: date
    ) -> list[dict[str, Any]] | None:
        urls = self._candidate_urls_for(target_date)
        if urls is None:
            return None

        fallback_rows: list[dict[str, Any]] | None = None
        for url in urls:
            rows = await self._ensure_payload(url)
            matching_rows = [r for r in rows if r["event_date"] == target_date]
            if self._payload_covers_date(rows, target_date):
                return matching_rows
            if matching_rows:
                return matching_rows
            fallback_rows = matching_rows

        return fallback_rows or []


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
            # ForexFactory's public XML feed emits clock times in UTC/GMT.
            # Do not interpret them as US/Eastern; doing so shifts high-impact
            # US releases (for example 12:30pm UTC PPI/CPI) four hours late.
            release_utc = release_local.replace(tzinfo=FOREXFACTORY_FEED_TZ)
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
    *,
    cache: ForexFactoryWeeklyCache | None = None,
) -> list[dict[str, Any]] | None:
    """Return ForexFactory rows whose ET-day == target_date.

    Returns None when target_date is outside the upstream rolling window.
    Raises ForexFactoryFetchError on retry exhaustion or transport error.
    Falls back to the legacy _fetch_xml_documents path when called without a
    cache (backwards-compat with existing tests that patch _fetch_xml_documents).
    """
    if cache is not None:
        return await cache.get_events_for_date(target_date)

    # Legacy path: used by existing tests that patch _fetch_xml_documents.
    try:
        documents = await _fetch_xml_documents(target_date)
    except Exception as exc:
        logger.warning("forexfactory fetch failed for %s: %s", target_date, exc)
        raise

    rows: list[dict[str, Any]] = []
    for xml_text in documents:
        rows.extend(_parse_one_xml(xml_text))

    return [r for r in rows if r["event_date"] == target_date]
