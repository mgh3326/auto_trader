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


def classify_investor_flow_partition(
    *,
    snapshot_date: dt.date,
    collected_at: dt.datetime | None,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    """Classify an investor_flow_snapshots partition's freshness.

    Mirrors classify_state() but without closes_window_len (investor_flow rows have
    no candle window). Primary staleness check: snapshot_date must match
    today_trading_date_value (which already rolls weekends back to Friday).
    Secondary age guard: mirrors classify_state() to catch orphaned partitions
    stamped with today's trading date but written long ago. Stays in this module
    so KST/trading-date logic lives in one place per ROB-277 §D4.
    """
    if snapshot_date != today_trading_date_value:
        return "stale"
    if collected_at is not None:
        age_hours = (now - collected_at).total_seconds() / 3600.0
        if age_hours >= STALE_AFTER_HOURS:
            return "stale"
    return "fresh"


def compute_overall_state(
    *,
    primary_state: DataState,
    dependency_states: list[DataState],
) -> DataState:
    """Aggregate primary + dependency states per ROB-277 §D1.c.

    NOT the same as aggregate_states(): when primary is fresh but a dependency is
    stale/missing, the user-visible overall is "stale" (conservative), not
    "fallback".
    """
    if primary_state in {"missing", "stale"}:
        return primary_state
    if any(s in {"missing", "stale"} for s in dependency_states):
        return "stale"
    if any(s == "partial" for s in dependency_states):
        return "partial"
    return primary_state


def format_kst_as_of_label(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime | None,
) -> str:
    """Format a Korean 'as-of' label for the data basis.

    With computed_at: "YYYY.MM.DD HH:MM 기준" in KST.
    Without computed_at: "YYYY.MM.DD 장마감 기준" (end-of-day partition).
    """
    if computed_at is None:
        return f"{snapshot_date.strftime('%Y.%m.%d')} 장마감 기준"
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    kst = computed_at.astimezone(ZoneInfo("Asia/Seoul"))
    return kst.strftime("%Y.%m.%d %H:%M 기준")
