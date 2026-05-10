"""Enumerates `(source, category, market)` triples expected to have data on a given date.

Pure function — no DB, no I/O. Used by the freshness service to distinguish
"never ingested" from "ingested but legitimately empty."

Weekend handling:
* US markets are closed Saturday + Sunday (UTC weekday 5 / 6); finnhub
  earningsCalendar still returns rows on weekends in rare cases (e.g. Berkshire
  weekend release) but for "expected" purposes we treat US weekends as not
  expected.
* KR markets follow the same Sat/Sun rule. We do not yet model KRX/NYSE
  observed holidays — the freshness signal for those days will simply show
  "no expected partition" rather than "missing." That's fine for the diagnostic
  surface; the dedicated KR-holidays source is a follow-up tracked in
  `docs/runbooks/calendar-source-coverage.md`.
* ForexFactory publishes a "this week" XML that always contains the upcoming
  five business days; we treat it as expected every day.
"""

from __future__ import annotations

from datetime import date

# All triples we currently know how to ingest (mirrors `SUPPORTED` in
# `scripts/ingest_market_events.py`).
EXPECTED_SOURCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
    }
)


def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Saturday = 5, Sunday = 6 in `date.weekday()`.
    """
    weekday = target_date.weekday()
    is_weekend = weekday >= 5

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if not is_weekend:
        triples.add(("finnhub", "earnings", "us"))
        triples.add(("dart", "disclosure", "kr"))
    return frozenset(triples)
