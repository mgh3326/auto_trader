"""hk 6145: coverage / latency math for the session minute tables (pure).

Per symbol x venue x session_segment for one session date:

* ``rows``              — distinct stored minutes
* ``expected_minutes``  — minutes of that segment that were already closed at ``as_of``
* ``missing_minutes``   — expected minutes with no stored row
* ``latency_p50/p95_s`` — ``retrieved_at - (time_utc + 1 min)`` in seconds

The expected set comes from ``session_segment_of`` itself, so the diagnostic
and the collector can never disagree about where a segment begins or ends.
Backfilled rows are excluded by the caller's SQL (``is_backfill_batch``): their
``retrieved_at`` is a promotion time, not a first receipt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.services.research_candles.session_minutes_collector import (
    BAR,
    CLOSE_GRACE,
)
from app.services.research_candles.session_segment import (
    KR_VENUES,
    UNKNOWN,
    session_segment_of,
    us_session_date,
)

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

#: batch ids that are not a first receipt (promotion from public, explicit backfill).
BACKFILL_BATCH_LIKE = ("%backfill%", "promote-%")


def is_backfill_batch(batch_id: str) -> bool:
    lowered = batch_id.lower()
    return "backfill" in lowered or lowered.startswith("promote-")


@dataclass(frozen=True, slots=True)
class StoredMinute:
    symbol: str
    venue: str
    session_segment: str
    time_utc: datetime
    retrieved_at: datetime


def _day_minutes(market: str, session_date: date) -> list[datetime]:
    if market == "kr":
        start = datetime.combine(session_date, time(7, 0), tzinfo=_KST)
        count = 14 * 60
    else:
        start = datetime.combine(
            session_date - timedelta(days=1), time(20, 0), tzinfo=_ET
        )
        count = 24 * 60
    first = start.astimezone(UTC)
    return [first + timedelta(minutes=i) for i in range(count)]


@lru_cache(maxsize=64)
def expected_minutes_by_segment(
    market: str, venue: str, session_date: date
) -> dict[str, frozenset[datetime]]:
    """Every minute start of the session date, grouped by its (non-UNKNOWN) segment."""
    grouped: dict[str, set[datetime]] = defaultdict(set)
    for minute in _day_minutes(market, session_date):
        if market == "us" and us_session_date(minute) != session_date:
            continue
        segment = session_segment_of(market, venue, minute)
        if segment != UNKNOWN:
            grouped[segment].add(minute)
    return {segment: frozenset(minutes) for segment, minutes in grouped.items()}


def percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile of an ascending list (``q`` in 0..1)."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def coverage_report(
    rows: Iterable[StoredMinute],
    *,
    market: str,
    session_date: date,
    as_of: datetime,
    requested_symbols: Sequence[str] = (),
) -> dict[str, object]:
    by_pair: dict[tuple[str, str], list[StoredMinute]] = defaultdict(list)
    for row in rows:
        by_pair[(row.symbol, row.venue)].append(row)

    entries: list[dict[str, object]] = []
    for (symbol, venue), stored in sorted(by_pair.items()):
        expected = expected_minutes_by_segment(market, venue, session_date)
        seen: dict[str, dict[datetime, StoredMinute]] = defaultdict(dict)
        for row in stored:
            seen[row.session_segment].setdefault(row.time_utc, row)
        for segment in sorted(set(expected) | set(seen)):
            closed_expected = {
                minute
                for minute in expected.get(segment, frozenset())
                if minute + BAR + CLOSE_GRACE <= as_of
            }
            present = seen.get(segment, {})
            latencies = sorted(
                (r.retrieved_at - (r.time_utc + BAR)).total_seconds()
                for r in present.values()
            )
            entries.append(
                {
                    "symbol": symbol,
                    "venue": venue,
                    "session_segment": segment,
                    "rows": len(present),
                    "expected_minutes": len(closed_expected),
                    "missing_minutes": len(closed_expected - set(present)),
                    "latency_p50_s": _round(percentile(latencies, 0.5)),
                    "latency_p95_s": _round(percentile(latencies, 0.95)),
                }
            )

    with_rows = {symbol for symbol, _ in by_pair}
    return {
        "market": market,
        "session_date": session_date.isoformat(),
        "as_of": as_of.astimezone(UTC).isoformat(),
        "venues_expected": list(KR_VENUES) if market == "kr" else None,
        "entries": entries,
        "symbols_without_rows": sorted(set(requested_symbols) - with_rows),
    }
