"""MFE/MAE horizon windows counted in exchange sessions (hk 5495).

A horizon of ``N`` is the next ``N`` exchange sessions after the entry session
(KR = XKRX, US = XNYS, crypto = consecutive UTC days), and ``to_exit`` runs
through the exit session. A value is given only when every session in that
window has closed and carries exactly one bar. The window is never filled by
counting stored bars: a missing bar must not shift a horizon onto later
sessions. Sessions come from the shared calendar in
``app.services.market_events.session_calendar``. A day the calendar cannot
classify is unknown, never guessed as a session or a holiday.

The metric itself (``metrics.calculate_mfe_mae``) is unchanged. This module only
decides which bars it may see and reports the window as unknown or pending.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal

from app.services.market_events.session_calendar import (
    Market,
    regular_session_bounds,
    trading_session_status,
    trading_sessions_in_range,
)
from app.services.retro_v1.daily_bars import Bar
from app.services.retro_v1.metrics import (
    DailyBar,
    Excursion,
    MfeMae,
    calculate_mfe_mae,
)

MarketKey = Literal["kr", "us", "crypto"]
Horizon = int | Literal["to_exit"]

_ONE_DAY: Final = timedelta(days=1)
#: Widest calendar span searched for N sessions. Twenty sessions fit in about a
#: month, so this only bounds a broken calendar, it is never a normal limit.
_MAX_SPAN_DAYS: Final = 400


def _calendar_market(market: MarketKey) -> Market:
    if market == "crypto":
        raise ValueError("crypto has no exchange calendar")
    return market


def _sessions_between(market: MarketKey, start: date, end: date) -> list[date] | None:
    """Sessions in ``[start, end]``; ``None`` when the calendar cannot classify it.

    ``trading_sessions_in_range`` returns an empty list on a calendar error, the
    same as a range with no sessions. The endpoints are classified first so a
    calendar failure is never read as an empty window.
    """
    if start > end:
        return []
    if market == "crypto":
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    cal = _calendar_market(market)
    if "unknown" in (
        trading_session_status(cal, start),
        trading_session_status(cal, end),
    ):
        return None
    return trading_sessions_in_range(cal, start, end)


def _required_sessions(
    market: MarketKey,
    entry: date,
    exit_session: date | None,
    horizon: Horizon,
) -> list[date] | None:
    """Sessions whose bars the horizon needs, in order; ``None`` when unknown.

    The exit session is included when it falls inside the window, because the
    window is only known to be complete once the exit day has closed and
    carries its bar. The metric itself still excludes the exit session.
    """
    if horizon == "to_exit":
        if exit_session is None:
            return None
        return _sessions_between(market, entry + _ONE_DAY, exit_session)
    span = 3 * horizon + 14
    for days in (span, 3 * span, _MAX_SPAN_DAYS):
        found = _sessions_between(market, entry + _ONE_DAY, entry + timedelta(days))
        if found is None:
            return None
        if len(found) >= horizon:
            window = found[:horizon]
            if exit_session is not None:
                window = [day for day in window if day <= exit_session]
            return window
    return None


def _closed(market: MarketKey, day: date, as_of: datetime) -> bool | None:
    """Whether ``day``'s session has ended by ``as_of``; ``None`` if unknown."""
    if market == "crypto":
        return as_of >= datetime.combine(day + _ONE_DAY, time.min, tzinfo=UTC)
    bounds = regular_session_bounds(_calendar_market(market), day)
    if bounds is None:
        return None
    return as_of >= bounds[1]


def _withheld(
    status: Literal["unknown", "pending"],
    reason: Literal["missing_data", "window_open"],
) -> MfeMae:
    empty = Excursion(None, None, None, None, status, reason)
    return MfeMae(empty, empty, None, "inference")


def excursion_pair(
    *,
    market: MarketKey,
    entry_price: Decimal,
    entry_session: date,
    exit_session: date | None,
    horizon: Horizon,
    bars_by_session: Mapping[date, Sequence[Bar]],
    as_of: datetime,
    risk_per_unit: Decimal | None,
) -> MfeMae:
    """MFE/MAE for one horizon over exchange sessions, withheld when incomplete.

    ``bars_by_session`` maps each session date to the bars stored for it, from one
    venue already chosen by the caller. Bars on non-session days are ignored.
    """
    if horizon == "to_exit" and exit_session is None:
        return _withheld("pending", "window_open")
    required = _required_sessions(market, entry_session, exit_session, horizon)
    if required is None:
        return _withheld("unknown", "missing_data")
    series: list[DailyBar] = []
    for day in required:
        closed = _closed(market, day, as_of)
        if closed is None:
            return _withheld("unknown", "missing_data")
        if not closed:
            return _withheld("pending", "window_open")
        bars = bars_by_session.get(day, ())
        if len(bars) != 1 or bars[0].high is None or bars[0].low is None:
            return _withheld("unknown", "missing_data")
        series.append(DailyBar(session=day, high=bars[0].high, low=bars[0].low))
    return calculate_mfe_mae(
        entry_price=entry_price,
        entry_session=entry_session,
        bars=series,
        exit_session=exit_session,
        horizon_sessions=horizon,
        risk_per_unit=risk_per_unit,
    )
