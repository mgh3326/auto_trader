"""Closed vocabulary and rendering for provider publication times (hk 6968).

Pure helpers shared by the news ingest schema, the payload normalizer and the
``get_news`` / ``get_disclosures`` MCP responses. No DB, network or clock read.
A time that is not known stays ``None``; nothing here invents one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from app.core.timezone import KST

# Precisions the news writers may store in ``news_articles.published_precision``.
NEWS_PUBLISHED_PRECISIONS: tuple[str, ...] = ("second", "minute", "date")

# ``±HH:MM``: the only shape ``news_articles.published_offset`` (VARCHAR(6))
# and :func:`offset_timezone` accept. ASCII digits only.
_OFFSET_RE = re.compile(r"([+-])([01][0-9]|2[0-3]):([0-5][0-9])")


def is_valid_offset(value: object) -> bool:
    return isinstance(value, str) and _OFFSET_RE.fullmatch(value) is not None


def offset_timezone(value: object) -> timezone | None:
    if not isinstance(value, str):
        return None
    match = _OFFSET_RE.fullmatch(value)
    if match is None:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-delta if sign == "-" else delta)


def offset_of(value: datetime) -> str | None:
    """``±HH:MM`` of an aware datetime, or None (naive or sub-minute offset)."""
    raw = value.strftime("%z")
    if len(raw) != 5:
        return None
    formatted = f"{raw[:3]}:{raw[3:]}"
    return formatted if is_valid_offset(formatted) else None


def _market_zone(market: str) -> tzinfo:
    """Zone a zone-less stored publication value is read in (Naver is KST)."""
    return KST if market == "kr" else UTC


def render_publication(
    value: datetime | None,
    *,
    offset: str | None,
    precision: str | None,
    market: str,
) -> str | None:
    """ISO 8601 publication time in the provider's offset, or None if unknown.

    ``precision == "date"`` renders a bare date: no time of day is claimed.
    Without a recorded offset the instant is shown in the market zone (KR: KST,
    otherwise UTC); the caller still reports the offset itself as unknown.
    """
    if value is None:
        return None
    zone = offset_timezone(offset) or _market_zone(market)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_market_zone(market))
    local = value.astimezone(zone)
    if precision == "date":
        return local.date().isoformat()
    return local.isoformat()


def render_instant_utc(value: datetime | None) -> str | None:
    """Our receipt clock as an aware UTC ISO string (naive values are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
