from __future__ import annotations

import datetime as dt

from app.schemas.invest_coverage import CoverageState

_FRESHNESS_WINDOWS_MINUTES = {"kr": 30, "us": 30, "crypto": 10}


def freshness_window_minutes(market: str) -> int:
    return _FRESHNESS_WINDOWS_MINUTES[market.strip().lower()]


def quote_state(
    market: str, latest_at: dt.datetime | None, now: dt.datetime
) -> CoverageState:
    if latest_at is None:
        return "missing"
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    window = dt.timedelta(minutes=freshness_window_minutes(market))
    return "fresh" if now - latest_at <= window else "stale"
