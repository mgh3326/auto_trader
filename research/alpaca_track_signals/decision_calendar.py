"""ROB-1061 H3 (AC1, Run A SS11.2/SS12.2) — the AP-A1/AP-A2 decision-time
calendar and the "prior completed UTC day" boundary.

    AP-A1: every day, 00:05:00 UTC.
    AP-A2: every Monday, 00:05:00 UTC.
    C_t = the close of the immediately preceding UTC day [00:00,24:00) --
        the decision at day D's 00:05 UTC consumes day (D-1)'s close, NEVER
        day D's own (still in-progress) data.

Every timestamp here is an explicit caller-supplied epoch-millisecond
``int`` — no wall clock, no ``datetime.now``/``utcnow``/``today`` anywhere in
this module (mirrors ``alpaca_track.daily_bars``'s discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = [
    "DAY_MS",
    "DECISION_HOUR_UTC",
    "DECISION_MINUTE_UTC",
    "is_ap_a1_decision_ts",
    "is_ap_a2_decision_ts",
    "prior_completed_day_window",
]

DAY_MS = 86_400_000
DECISION_HOUR_UTC = 0
DECISION_MINUTE_UTC = 5


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def is_ap_a1_decision_ts(ts_ms: int) -> bool:
    """True iff ``ts_ms`` is EXACTLY 00:05:00.000 UTC on some calendar day."""
    _int(ts_ms, "ts_ms")
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return (
        dt.hour == DECISION_HOUR_UTC
        and dt.minute == DECISION_MINUTE_UTC
        and dt.second == 0
        and dt.microsecond == 0
    )


def is_ap_a2_decision_ts(ts_ms: int) -> bool:
    """True iff ``ts_ms`` is EXACTLY 00:05:00.000 UTC on a Monday."""
    _int(ts_ms, "ts_ms")
    if not is_ap_a1_decision_ts(ts_ms):
        return False
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return dt.weekday() == 0  # Monday


def prior_completed_day_window(decision_ts_ms: int) -> tuple[int, int]:
    """The half-open ``[start_ms, end_ms)`` UTC-day window whose close is
    ``C_t`` for a decision at ``decision_ts_ms``.

    A decision at day D's 00:05 UTC consumes day (D-1)'s
    ``[00:00,24:00)`` window — ``end_ms`` is exactly day D's own 00:00 UTC
    (D's own, still in-progress, day is NEVER consumed; AC1/AC2). Requires
    ``decision_ts_ms`` to actually be an AP-A1 (daily) decision timestamp —
    AP-A2's weekly decision consumes the SAME prior-day boundary (the most
    recently completed UTC day), so this same function serves both.
    """
    if not is_ap_a1_decision_ts(decision_ts_ms):
        raise ValueError(
            f"{decision_ts_ms} is not a 00:05:00 UTC decision timestamp"
        )
    day_start_of_decision_day = (decision_ts_ms // DAY_MS) * DAY_MS
    end_ms = day_start_of_decision_day
    start_ms = end_ms - DAY_MS
    return start_ms, end_ms
