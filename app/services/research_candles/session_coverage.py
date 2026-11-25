"""hk 6145: coverage / latency math for the session minute tables (pure).

Per symbol x venue x session_segment for one session date:

* ``rows``              — distinct stored minutes
* ``expected_minutes``  — minutes of that segment that were already closed at ``as_of``
* ``missing_minutes``   — expected minutes with no stored row
* ``value_null`` / ``value_null_ratio`` — stored minutes whose KR ``value`` is NULL
  (not provable from the cumulative KIS value; a cagg SUM over them is partial)
* ``latency_p50/p95_s`` — ``retrieved_at - (time_utc + 1 min)`` in seconds

The expected set comes from ``session_segment_of`` itself, so the diagnostic
and the collector can never disagree about where a segment begins or ends.
Backfilled rows are excluded by the caller's SQL (``is_backfill_batch``): their
``retrieved_at`` is a promotion time, not a first receipt.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
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
    value_is_null: bool = False


def _day_minutes(market: str, session_date: date) -> list[datetime]:
    if market == "futures":
        # Day session only (hk 6148): no night TR is documented, so night minutes
        # are not expected (night rows, if any, are reported with expected 0).
        start = datetime.combine(session_date, time(8, 0), tzinfo=_KST)
        count = 9 * 60
    elif market == "kr":
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


#: Closed reasons for a requested symbol that has no stored minute (hk 6299).
ZERO_ROW_REASONS = (
    "not_selected",
    "cut_by_budget",
    "fetch_error",
    "no_trades_reported",
)


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """What one collector run (``collect_session_minutes`` JSON) says about symbols."""

    targeted: frozenset[str]
    cut: frozenset[str]
    error_symbols: frozenset[str]


def run_evidence_from_report(report: Mapping[str, object]) -> RunEvidence:
    """Read a collector run report; fails when the run did not list its targets.

    A ``dry_run`` report writes nothing and proves no read of the day: it gives no
    evidence at all (its target list is still validated).
    """
    targeted = report.get("targeted_symbols")
    cuts = report.get("targets_cuts") or []
    errors = report.get("error_details") or []
    if not isinstance(targeted, list):
        raise ValueError("run report has no targeted_symbols list")
    if not isinstance(cuts, list) or not isinstance(errors, list):
        raise ValueError("run report has malformed targets_cuts or error_details")
    if report.get("dry_run") is True:
        return RunEvidence(frozenset(), frozenset(), frozenset())
    return RunEvidence(
        targeted=frozenset(str(s) for s in targeted),
        cut=frozenset(
            str(item["symbol"])
            for item in cuts
            if isinstance(item, Mapping) and item.get("reason") == "budget_cut"
        ),
        error_symbols=frozenset(
            str(item["symbol"])
            for item in errors
            if isinstance(item, Mapping) and item.get("symbol")
        ),
    )


def classify_zero_row(symbol: str, runs: Sequence[RunEvidence]) -> str:
    """One of ``ZERO_ROW_REASONS``, by precedence:

    * an error on any run: a failed read is not proof of "no trades";
    * a budget cut on any run: the symbol was left out during the session, even if
      an earlier (e.g. pre-open) run targeted it and could not have stored rows for
      the date;
    * targeted on some run, never cut: it was requested and read, no minute stored;
    * otherwise it was never in a run's targets.
    """
    if any(symbol in run.error_symbols for run in runs):
        return "fetch_error"
    if any(symbol in run.cut for run in runs):
        return "cut_by_budget"
    if any(symbol in run.targeted for run in runs):
        return "no_trades_reported"
    return "not_selected"


@dataclass(frozen=True, slots=True)
class EdgeRow:
    """One stored bar for the first/last check (hk 6322): bar START, no value."""

    symbol: str
    source: str
    venue: str
    session_segment: str
    time_utc: datetime


def first_last_report(
    rows: Iterable[EdgeRow], *, market: str, session_date: date
) -> dict[str, object]:
    """First and last stored bar per symbol x source x venue x segment.

    This is the check dots asks for ("첫 봉·마지막 봉"): for one session date, the
    earliest and the latest bar START of every group, with its KST rendering. Rows
    are not deduplicated across sources: a Toss row and a KIS row are two groups.
    """
    groups: dict[tuple[str, str, str, str], list[datetime]] = defaultdict(list)
    for row in rows:
        groups[(row.symbol, row.source, row.venue, row.session_segment)].append(
            row.time_utc
        )
    entries: list[dict[str, object]] = []
    for (symbol, source, venue, segment), stamps in sorted(groups.items()):
        first, last = min(stamps), max(stamps)
        entries.append(
            {
                "symbol": symbol,
                "source": source,
                "venue": venue,
                "session_segment": segment,
                "rows": len(stamps),
                "first_bar_start_utc": first.astimezone(UTC).isoformat(),
                "first_bar_start_kst": first.astimezone(_KST).isoformat(),
                "last_bar_start_utc": last.astimezone(UTC).isoformat(),
                "last_bar_start_kst": last.astimezone(_KST).isoformat(),
            }
        )
    return {
        "mode": "first_last",
        "market": market,
        "session_date": session_date.isoformat(),
        "bar_start_note": "time_utc is the bar START; Toss timestamps are bar ENDs",
        "entries": entries,
    }


def coverage_report(
    rows: Iterable[StoredMinute],
    *,
    market: str,
    session_date: date,
    as_of: datetime,
    requested_symbols: Sequence[str] = (),
    run_evidence: Sequence[RunEvidence] | None = None,
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
            value_null = sum(1 for r in present.values() if r.value_is_null)
            entries.append(
                {
                    "symbol": symbol,
                    "venue": venue,
                    "session_segment": segment,
                    "rows": len(present),
                    "value_null": value_null,
                    "value_null_ratio": (
                        round(value_null / len(present), 4) if present else None
                    ),
                    "expected_minutes": len(closed_expected),
                    "missing_minutes": len(closed_expected - set(present)),
                    "latency_p50_s": _round(percentile(latencies, 0.5)),
                    "latency_p95_s": _round(percentile(latencies, 0.95)),
                }
            )

    with_rows = {symbol for symbol, _ in by_pair}
    missing = sorted(set(requested_symbols) - with_rows)
    report: dict[str, object] = {
        "market": market,
        "session_date": session_date.isoformat(),
        "as_of": as_of.astimezone(UTC).isoformat(),
        "venues_expected": list(KR_VENUES) if market == "kr" else None,
        "night_collection": "unsupported" if market == "futures" else None,
        "entries": entries,
        "symbols_without_rows": missing,
    }
    if run_evidence is not None:
        report["requested_zero_row"] = {
            symbol: classify_zero_row(symbol, run_evidence) for symbol in missing
        }
    return report
