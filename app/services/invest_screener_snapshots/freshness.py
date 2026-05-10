from __future__ import annotations

import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from app.services.market_events.freshness_service import STALE_AFTER_HOURS

DataState = Literal["fresh", "partial", "stale", "missing", "fallback"]

_TZ_BY_MARKET = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}
_PARTIAL_MAX_LEN = (
    5  # closes_window length < 5 → partial (week_change_rate not computable)
)


def today_trading_date(market: str, *, now: dt.datetime | None = None) -> dt.date:
    """Most recent business day in the market's timezone.

    NOTE: First-slice does NOT use exchange holiday calendar. KIS daily candles
    already collapse Korean public holidays into the previous trading day close,
    so this approximation is safe for snapshot freshness classification.
    """
    tz = _TZ_BY_MARKET.get(market, _TZ_BY_MARKET["kr"])
    now_local = (now or dt.datetime.now(dt.UTC)).astimezone(tz)
    candidate = now_local.date()
    while candidate.weekday() >= 5:
        candidate -= dt.timedelta(days=1)
    return candidate


def classify_state(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime,
    closes_window_len: int,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    age_hours = (now - computed_at).total_seconds() / 3600.0
    if snapshot_date != today_trading_date_value or age_hours >= STALE_AFTER_HOURS:
        return "stale"
    if closes_window_len < 2:
        return "missing"  # not really usable; treat as absent
    if closes_window_len < _PARTIAL_MAX_LEN:
        return "partial"
    return "fresh"


_PRIORITY: dict[DataState, int] = {
    "missing": 0,
    "fallback": 1,
    "stale": 2,
    "partial": 3,
    "fresh": 4,
}


def aggregate_states(states: list[DataState]) -> DataState:
    if not states:
        return "missing"
    has_missing = "missing" in states
    has_fresh_or_partial = any(s in {"fresh", "partial"} for s in states)
    if has_missing and has_fresh_or_partial:
        return "fallback"
    return min(states, key=lambda s: _PRIORITY[s])
