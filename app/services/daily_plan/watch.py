"""Read-time lifecycle for plan-only watch rows."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class WatchState:
    state: Literal["active", "queued", "closed", "calendar_unknown"]
    carry_count: int
    next_session: date | None = None
    reason: str | None = None


def watch_state(
    *,
    asof: datetime,
    valid_until: datetime,
    carry_allowed: bool,
    carry_count: int,
    carry_max_sessions: int,
    next_session: date | None,
    session_date: date | None = None,
) -> WatchState:
    """Project an expired row into a single next-session reevaluation.

    A queued row never becomes active on the following date by itself. The
    session must publish a successor row before a further carry is possible.
    """
    if asof.utcoffset() is None or valid_until.utcoffset() is None:
        raise ValueError("unknown_time")
    if carry_count < 0 or carry_max_sessions < 0:
        raise ValueError("invalid_carry_count")
    if asof < valid_until:
        return WatchState("active", carry_count)
    if not carry_allowed or carry_count >= carry_max_sessions:
        return WatchState("closed", carry_count, reason="expired")
    if next_session is None:
        return WatchState("calendar_unknown", carry_count, reason="calendar_unknown")
    if session_date is not None and session_date > next_session:
        return WatchState("closed", carry_count, reason="expired")
    return WatchState("queued", carry_count + 1, next_session)
