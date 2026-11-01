"""Partition days and availability instants per market.

* KR/US: one partition per exchange session (exchange_calendars XKRX/XNYS,
  early closes included). The session-``P`` bar is available at the session
  close plus ``EQUITY_BAR_LAG``.
* crypto: one partition per UTC day. Partition ``P`` holds the candle that
  closed at ``P`` 00:00 UTC (the candle of day ``P-1``); it is available at
  00:00 UTC plus ``CRYPTO_BAR_LAG``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from .contract import CRYPTO_BAR_LAG, EQUITY_BAR_LAG, market_spec


@lru_cache(maxsize=4)
def _calendar(code: str):  # noqa: ANN202 - exchange_calendars type
    import exchange_calendars

    return exchange_calendars.get_calendar(code)


def is_partition_day(market: str, day: date) -> bool:
    spec = market_spec(market)
    if spec.calendar is None:
        return True
    cal = _calendar(spec.calendar)
    if not (cal.first_session.date() <= day <= cal.last_session.date()):
        raise ValueError(f"{day} is outside the {spec.calendar} calendar bounds")
    return bool(cal.is_session(day.isoformat()))


def partition_days(market: str, start: date, end: date) -> list[date]:
    """Partition days in ``[start, end]``."""
    spec = market_spec(market)
    if start > end:
        return []
    if spec.calendar is None:
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    cal = _calendar(spec.calendar)
    sessions = cal.sessions_in_range(start.isoformat(), end.isoformat())
    return [s.date() for s in sessions]


# KRX opens and closes one hour late on the national college entrance exam
# (CSAT) day; exchange_calendars' XKRX models those days as 09:00-15:30, which
# would let a forming bar pass the availability gate between 16:00 and 16:30.
# Add each year's date when it is announced (the rest of the repo has the same
# gap: see the 7-D night-sweep contract).
KRX_LATE_CLOSE_DAYS = frozenset(
    {date(2024, 11, 14), date(2025, 11, 13), date(2026, 11, 19)}
)
KRX_LATE_CLOSE_SHIFT = timedelta(hours=1)


def session_close(market: str, day: date) -> datetime:
    spec = market_spec(market)
    if spec.calendar is None:
        return datetime.combine(day, time(0, 0), tzinfo=UTC)
    if not is_partition_day(market, day):
        raise ValueError(f"{day} is not a {spec.calendar} session")
    close = _calendar(spec.calendar).session_close(day.isoformat()).to_pydatetime()
    if market == "kr" and day in KRX_LATE_CLOSE_DAYS:
        close += KRX_LATE_CLOSE_SHIFT
    return close


def bar_available_at(market: str, day: date) -> datetime:
    """When the bar stored in partition ``day`` becomes known."""
    if market_spec(market).calendar is None:
        return session_close(market, day) + CRYPTO_BAR_LAG
    return session_close(market, day) + EQUITY_BAR_LAG


def local_today(market: str, now: datetime) -> date:
    return now.astimezone(market_spec(market).tz).date()
