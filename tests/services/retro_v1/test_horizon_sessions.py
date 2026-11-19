"""Horizon windows count exchange sessions (hk 5495), not stored bars."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.retro_v1.daily_bars import Bar
from app.services.retro_v1.horizon_sessions import excursion_pair

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, tzinfo=UTC)
ENTRY_PRICE = Decimal("100")


def bar(high: str, low: str | None = None) -> Bar:
    return Bar(
        symbol="X",
        time=datetime(2026, 1, 1, tzinfo=UTC),
        open=None,
        high=Decimal(high),
        low=Decimal(low if low is not None else high),
        close=None,
        volume=None,
        venue="KRX",
        source="kis",
    )


def bars_for(*days: date, high: str = "101") -> dict[date, list[Bar]]:
    return {day: [bar(high)] for day in days}


def run(
    market: str,
    entry: date,
    horizon,
    bars: dict[date, list[Bar]],
    *,
    exit_session: date | None = None,
    as_of: datetime = NOW,
):
    return excursion_pair(
        market=market,
        entry_price=ENTRY_PRICE,
        entry_session=entry,
        exit_session=exit_session,
        horizon=horizon,
        bars_by_session=bars,
        as_of=as_of,
        risk_per_unit=None,
    )


def kr_sessions_after_jan5(count: int) -> list[date]:
    """XKRX sessions Jan 6..: no holiday in this window, weekends skipped."""
    days: list[date] = []
    day = date(2026, 1, 6)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


def test_kr_horizon_skips_weekend_and_holiday_without_treating_them_as_gaps() -> None:
    # 2026-01-01 is an XKRX holiday and Jan 3-4 are a weekend. Entry is the last
    # session of 2025, so the next five sessions are Jan 2, 5, 6, 7, 8.
    sessions = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    sessions += [date(2026, 1, 7), date(2026, 1, 8)]
    bars = bars_for(*sessions, high="103")
    bars[date(2026, 1, 9)] = [bar("999")]  # outside the window: never used
    pair = run("kr", date(2025, 12, 31), 5, bars)
    assert pair.mfe.status == "complete"
    assert pair.mfe.amount == Decimal("3")
    assert pair.mae.amount == Decimal("0")


def test_kr_missing_session_makes_the_horizon_unknown_not_shifted() -> None:
    days = kr_sessions_after_jan5(20)
    bars = bars_for(*(d for d in days if d != date(2026, 1, 9)))
    one = run("kr", date(2026, 1, 5), 1, bars)
    five = run("kr", date(2026, 1, 5), 5, bars)
    assert one.mfe.status == "complete"
    assert five.mfe.status == "unknown" and five.mfe.reason == "missing_data"
    assert five.mae.status == "unknown" and five.mae.reason == "missing_data"
    assert five.mfe.amount is None and five.mfe.pct is None and five.mfe.r is None


def test_to_exit_with_a_missing_session_before_exit_is_unknown() -> None:
    days = kr_sessions_after_jan5(20)
    bars = bars_for(*(d for d in days if d != date(2026, 1, 8)))
    pair = run("kr", date(2026, 1, 5), "to_exit", bars, exit_session=days[9])
    assert pair.mfe.status == "unknown" and pair.mfe.reason == "missing_data"


def test_us_presidents_day_is_not_a_session_gap() -> None:
    # Fri 2026-02-13 entry; Mon 2026-02-16 is Presidents' Day (XNYS closed).
    sessions = [date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19)]
    sessions += [date(2026, 2, 20), date(2026, 2, 23)]
    complete = run("us", date(2026, 2, 13), 5, bars_for(*sessions))
    assert complete.mfe.status == "complete"
    gap = run(
        "us", date(2026, 2, 13), 5, bars_for(*(d for d in sessions if d != sessions[1]))
    )
    assert gap.mfe.status == "unknown" and gap.mfe.reason == "missing_data"


def test_crypto_counts_every_utc_day_including_weekends() -> None:
    # Crypto has no exchange calendar: Sat 2026-01-10 is a session day too.
    days = [date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    days += [date(2026, 1, 9), date(2026, 1, 10)]
    complete = run("crypto", date(2026, 1, 5), 5, bars_for(*days))
    assert complete.mfe.status == "complete"
    gap = run("crypto", date(2026, 1, 5), 5, bars_for(*(d for d in days if d.day != 8)))
    assert gap.mfe.status == "unknown" and gap.mfe.reason == "missing_data"


def test_horizon_whose_last_session_has_not_closed_is_pending_not_a_value() -> None:
    days = kr_sessions_after_jan5(5)  # Jan 6, 7, 8, 9, 12
    bars = bars_for(*days)
    # XKRX closes 15:30 KST = 06:30 UTC; Jan 12 has not closed yet at 06:29 UTC.
    before = datetime(2026, 1, 12, 6, 29, tzinfo=UTC)
    at_close = datetime(2026, 1, 12, 6, 30, tzinfo=UTC)
    pending = run("kr", date(2026, 1, 5), 5, bars, as_of=before)
    assert pending.mfe.status == "pending" and pending.mfe.reason == "window_open"
    assert pending.mfe.amount is None and pending.mae.status == "pending"
    closed = run("kr", date(2026, 1, 5), 5, bars, as_of=at_close)
    assert closed.mfe.status == "complete"


def test_crypto_day_is_closed_at_the_next_utc_midnight() -> None:
    days = [date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    days += [date(2026, 1, 9), date(2026, 1, 10)]
    bars = bars_for(*days)
    before = datetime(2026, 1, 10, 23, 59, tzinfo=UTC)
    after = datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
    assert (
        run("crypto", date(2026, 1, 5), 5, bars, as_of=before).mfe.status == "pending"
    )
    assert (
        run("crypto", date(2026, 1, 5), 5, bars, as_of=after).mfe.status == "complete"
    )


def test_to_exit_without_an_exit_session_is_pending() -> None:
    pair = run("kr", date(2026, 1, 5), "to_exit", bars_for(date(2026, 1, 6)))
    assert pair.mfe.status == "pending" and pair.mfe.reason == "window_open"


def test_exit_session_inside_the_horizon_stops_the_window_there() -> None:
    # Exit on Jan 7: the window is Jan 6 only. Jan 7 is included as a required
    # session but its high (999) is never used, as the metric excludes it.
    bars = bars_for(date(2026, 1, 6), high="103")
    bars[date(2026, 1, 7)] = [bar("999")]
    pair = run("kr", date(2026, 1, 5), 5, bars, exit_session=date(2026, 1, 7))
    assert pair.mfe.status == "complete" and pair.mfe.amount == Decimal("3")


def test_a_session_with_two_bars_is_not_exactly_one_and_is_unknown() -> None:
    bars = bars_for(date(2026, 1, 6))
    bars[date(2026, 1, 6)] = [bar("103"), bar("104")]
    pair = run("kr", date(2026, 1, 5), 1, bars)
    assert pair.mfe.status == "unknown" and pair.mfe.reason == "missing_data"


def test_a_day_the_calendar_cannot_classify_is_unknown_not_guessed() -> None:
    pair = run("kr", date(1900, 1, 2), 1, {})
    assert pair.mfe.status == "unknown" and pair.mfe.reason == "missing_data"
