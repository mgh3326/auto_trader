"""Fail-closed KRX regular-session gate for new runner entries."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.market_events.session_calendar import regular_session_bounds


def is_krx_regular_session(moment: datetime) -> bool:
    """True only inside a confirmed XKRX regular session.

    The shared XKRX calendar handles weekends, holidays, and calendar failures
    fail-closed.  NXT/SOR windows are deliberately not considered here.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    utc_moment = moment.astimezone(UTC)
    bounds = regular_session_bounds("kr", utc_moment.date())
    if bounds is None:
        return False
    open_at, close_at = bounds
    return open_at <= utc_moment < close_at
