"""Per-day TradingView economic-calendar fetch helper (ROB-210).

Fetches events from the TradingView economic-calendar endpoint, parses the JSON
response, and filters to rows whose UTC-day matches the requested target_date.

The endpoint is treated as an internal web endpoint, not an official public API.
ForexFactory remains the fallback/cross-check source.

Caller passes the resulting rows into
`app.services.market_events.normalizers.normalize_tradingview_event_row`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TRADINGVIEW_CALENDAR_URL = "https://economic-calendar.tradingview.com/events"
TRADINGVIEW_CALENDAR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/economic-calendar/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


async def _fetch_tradingview_raw(from_date: date, to_date: date) -> Any:
    """Fetch raw JSON payload from TradingView economic-calendar endpoint.

    Kept as a module-level seam for tests to patch.
    """
    params = {
        "from": f"{from_date.isoformat()}T00:00:00Z",
        "to": f"{to_date.isoformat()}T23:59:59Z",
        "country": "all",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            TRADINGVIEW_CALENDAR_URL,
            params=params,
            headers=TRADINGVIEW_CALENDAR_HEADERS,
        )
        response.raise_for_status()
        return response.json()


def _parse_tv_date(value: Any) -> datetime | None:
    """Parse TradingView date field: Unix timestamp (int/float) or ISO string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _parse_tv_rows(payload: Any) -> list[dict[str, Any]]:
    """Extract and normalize event rows from TradingView response payload."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("result") or payload.get("data") or []
        if not isinstance(items, list):
            items = []
    else:
        items = []

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dt = _parse_tv_date(item.get("date"))
        if dt is None:
            continue
        rows.append(
            {
                "id": item.get("id"),
                "title": (item.get("title") or "").strip(),
                "country": item.get("country"),
                "date_utc": dt,
                "period": item.get("period"),
                "actual": item.get("actual"),
                "forecast": item.get("forecast"),
                "previous": item.get("previous"),
                "unit": item.get("unit"),
                "source": item.get("source"),
                "source_url": item.get("source_url"),
                "ticker": item.get("ticker"),
                "importance": item.get("importance"),
                "_raw": dict(item),
            }
        )
    return rows


async def fetch_tradingview_events_for_date(
    target_date: date,
) -> list[dict[str, Any]]:
    """Return TradingView economic-calendar rows whose UTC-day == target_date.

    Raises on network/HTTP errors so the caller can mark the partition failed.
    Returns [] on JSON parse problems so one bad day doesn't block others.
    """
    try:
        payload = await _fetch_tradingview_raw(target_date, target_date)
    except Exception as exc:
        logger.warning("tradingview fetch failed for %s: %s", target_date, exc)
        raise

    rows = _parse_tv_rows(payload)
    return [r for r in rows if r["date_utc"].date() == target_date]
