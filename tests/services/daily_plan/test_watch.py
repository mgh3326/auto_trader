from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.daily_plan.watch import WatchState, watch_state

EXPIRY = datetime(2026, 11, 27, 6, 30, tzinfo=UTC)
NEXT_SESSION = date(2026, 11, 30)


@pytest.mark.parametrize(
    ("asof", "allowed", "count", "next_session", "expected"),
    [
        (EXPIRY - timedelta(seconds=1), True, 4, NEXT_SESSION, WatchState("active", 4)),
        (EXPIRY, True, 4, NEXT_SESSION, WatchState("queued", 5, NEXT_SESSION)),
        (EXPIRY, True, 5, NEXT_SESSION, WatchState("closed", 5, reason="expired")),
        (EXPIRY, False, 0, NEXT_SESSION, WatchState("closed", 0, reason="expired")),
        (
            EXPIRY,
            True,
            4,
            None,
            WatchState("calendar_unknown", 4, reason="calendar_unknown"),
        ),
    ],
)
def test_watch_expiry(asof, allowed, count, next_session, expected):
    assert (
        watch_state(
            asof=asof,
            valid_until=EXPIRY,
            carry_allowed=allowed,
            carry_count=count,
            carry_max_sessions=5,
            next_session=next_session,
        )
        == expected
    )


def test_watch_closes_after_missed_reevaluation_session():
    assert watch_state(
        asof=datetime(2026, 12, 1, tzinfo=UTC),
        valid_until=EXPIRY,
        carry_allowed=True,
        carry_count=0,
        carry_max_sessions=5,
        next_session=NEXT_SESSION,
        session_date=date(2026, 12, 1),
    ) == WatchState("closed", 0, reason="expired")
