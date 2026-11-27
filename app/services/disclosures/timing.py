"""Publish and first-receipt times for a DART filing (hk 6968).

Pure. Evidence comes from ``market_events``: the DART row (collector receipt
clock) and the ``naver_disclosure`` rows linked to the same receipt number.
Nothing is invented: a time we do not hold stays ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.core.timezone import KST
from app.services.publication_time import offset_timezone, render_instant_utc

PUBLISHED_FROM_EVENT = "market_events_publication"
PUBLISHED_FROM_DART_RECEIPT = "dart_receipt_time"
PUBLISHED_FROM_NAVER = "naver_disclosure"
PUBLISHED_FROM_DART_LIST = "dart_list_date"

_KST_OFFSET = "+09:00"
# ``list_date_ex`` receipt time: a date followed by HH:MM(:SS), zone-less KST.
_TIMED_RECEIPT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")
_HAS_SECONDS_RE = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}")


@dataclass(frozen=True)
class StoredDisclosureRow:
    source: str  # "dart" | "naver_disclosure"
    published_at: datetime | None = None
    published_date: date | None = None
    published_offset: str | None = None
    published_precision: str | None = None
    provider_created_at: datetime | None = None
    provider_created_raw: str | None = None
    first_received_at: datetime | None = None
    # DART rows only: ``raw_payload_json.rcept_dt``.
    receipt_text: str | None = None


def _publication(
    published_at: str, offset: str, precision: str, source: str
) -> dict[str, Any]:
    return {
        "published_at": published_at,
        "published_offset": offset,
        "published_precision": precision,
        "published_at_source": source,
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _kst(value: datetime) -> datetime:
    return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)


def _from_event_columns(row: StoredDisclosureRow) -> dict[str, Any] | None:
    precision = row.published_precision
    if precision == "date" and row.published_date is not None:
        return _publication(
            row.published_date.isoformat(),
            row.published_offset or _KST_OFFSET,
            precision,
            PUBLISHED_FROM_EVENT,
        )
    if precision and row.published_at is not None:
        zone = offset_timezone(row.published_offset)
        instant = _kst(row.published_at)
        if zone is not None:
            instant = instant.astimezone(zone)
        return _publication(
            instant.isoformat(),
            row.published_offset if zone is not None else _KST_OFFSET,
            precision,
            PUBLISHED_FROM_EVENT,
        )
    return None


def _from_dart_receipt(row: StoredDisclosureRow) -> dict[str, Any] | None:
    text = row.receipt_text
    if not text or not _TIMED_RECEIPT_RE.match(text):
        return None
    try:
        instant = _kst(datetime.fromisoformat(text))
    except ValueError:
        return None
    return _publication(
        instant.isoformat(), _KST_OFFSET, "minute", PUBLISHED_FROM_DART_RECEIPT
    )


def _published(
    filing_date: str | None, rows: Sequence[StoredDisclosureRow]
) -> dict[str, Any]:
    dart_rows = [row for row in rows if row.source == "dart"]
    for read in (_from_event_columns, _from_dart_receipt):
        for row in dart_rows:
            if found := read(row):
                return found
    relayed = sorted(
        (
            row
            for row in rows
            if row.source == "naver_disclosure" and row.provider_created_at is not None
        ),
        key=lambda row: _kst(row.provider_created_at),
    )
    if relayed:
        first = relayed[0]
        raw = first.provider_created_raw or ""
        return _publication(
            _kst(first.provider_created_at).isoformat(),
            _KST_OFFSET,
            "second" if _HAS_SECONDS_RE.search(raw) else "minute",
            PUBLISHED_FROM_NAVER,
        )
    if filing_date:
        return _publication(filing_date, _KST_OFFSET, "date", PUBLISHED_FROM_DART_LIST)
    return {
        "published_at": None,
        "published_offset": None,
        "published_precision": None,
        "published_at_source": None,
    }


def disclosure_timing(
    filing_date: str | None, rows: Sequence[StoredDisclosureRow]
) -> dict[str, Any]:
    """Response fields for one filing.

    ``first_received_at`` is the earliest receipt clock we stored for the filing
    from either source; rows stored before that clock existed contribute none.
    """
    first = min(
        (row for row in rows if row.first_received_at is not None),
        key=lambda row: _utc(row.first_received_at),
        default=None,
    )
    return {
        **_published(filing_date, rows),
        "first_received_at": render_instant_utc(first.first_received_at)
        if first
        else None,
        "first_received_source": first.source if first else None,
    }
