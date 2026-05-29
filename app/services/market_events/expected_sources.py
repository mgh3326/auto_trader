"""Enumerates `(source, category, market)` triples expected to have data on a given date.

Pure function — no DB, no I/O. Used by the freshness service to distinguish
"never ingested" from "ingested but legitimately empty."

Session handling (weekend + holiday aware as of ROB-371):
* US sources (finnhub) are gated on the NYSE (XNYS) trading calendar and KR
  sources (dart, wisefn) on the KRX (XKRX) calendar via
  :mod:`app.services.market_events.session_calendar`. Both weekends AND observed
  exchange holidays are excluded — on a holiday those sources are simply "not
  expected" (no false "missing" signal). This closes the prior follow-up that
  modelled weekends only; the freshness matrix now reads a missing holiday
  partition as *expected-absent*, not an ingest failure.
* finnhub earningsCalendar still returns rows on closed days in rare cases (e.g.
  Berkshire weekend release) but for "expected" purposes we follow the exchange
  calendar. Fail-closed: a day the calendar cannot confirm open is treated as
  closed, so a source is never claimed expected on a non-session day.
* ForexFactory publishes a "this week" XML that always contains the upcoming
  five business days; we treat it as expected every day (not session-gated).
* WiseFn KR earnings (ROB-171) is a forward-looking schedule source; we expect
  it on KR weekdays only, matching DART. The default fetcher raises
  NotImplementedError until the upstream contract is confirmed, so freshness
  for `(wisefn, earnings, kr)` will surface "expected but failed" until the
  helper is wired and `WISEFN_EARNINGS_ENABLED=true` is set.
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
        ("wisefn", "earnings", "kr"),
    }
)


def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Session-aware (ROB-371): US sources are gated on the XNYS trading calendar
    and KR sources on the XKRX calendar — both weekend- and holiday-aware.
    ForexFactory is expected every day. Fail-closed: a day the calendar cannot
    confirm open is treated as closed.
    """
    from app.services.market_events.session_calendar import is_trading_session

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if is_trading_session("us", target_date):
        triples.add(("finnhub", "earnings", "us"))
    if is_trading_session("kr", target_date):
        triples.add(("dart", "disclosure", "kr"))
        triples.add(("wisefn", "earnings", "kr"))
    return frozenset(triples)
