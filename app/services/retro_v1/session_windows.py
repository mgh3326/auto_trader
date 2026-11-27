"""Session windows of ``session_observation`` (hk 6421), on the bar START time.

The stored ``session_segment`` labels come from
``app.services.research_candles.session_segment``. That module imports a broker
and an MCP module, which the retro/v1 import guard forbids, so the same windows
are written here (a copy, like ``daily_bars.is_first_receipt_batch``) and
``test_session_observation.py`` pins every minute of them against
``session_segment_of``. Trading days and the XNYS regular bounds come from the
shared calendar; an unclassifiable day is unknown, never guessed.

Two windows are narrower than the stored labels, because the stored label of a
bar is not the window in which a bar can still arrive (hk 7002):

* the futures day session ends at 15:20 on the contract's last trading day
  (15:45 otherwise), so normal collection after that close is not ``delayed``;
* the US after-market ends at 17:00 ET when the exchange closes early (13:00 ET
  regular close), not at 20:00 ET — Nasdaq Equity Trader Alert #2026-69 and the
  NYSE holidays/trading-hours page both end their extended sessions at 5:00 p.m.
  ET on such a day.

Only the delay rule and ``session_end`` use these cuts: the stored
``session_segment`` of a bar keeps its own windows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import (
    Market,
    SessionStatus,
    regular_session_bounds,
    trading_session_status,
)

_KST: Final = ZoneInfo("Asia/Seoul")
_ET: Final = ZoneInfo("America/New_York")

#: KR venue -> its sessions as ``(session, start, end)`` KST clock times.
KR_SESSIONS: Final[dict[str, tuple[tuple[str, time, time], ...]]] = {
    "KRX": (
        ("KRX_REGULAR", time(9, 0), time(15, 30)),
        ("KRX_AFTER", time(16, 0), time(20, 0)),
    ),
    "NTX": (
        ("NXT_PRE", time(8, 0), time(8, 50)),
        ("NXT_OVERLAP", time(9, 0), time(15, 20)),
        ("NXT_POST", time(16, 0), time(20, 0)),
    ),
}
US_EXCHANGE_SESSIONS: Final = ("US_PRE", "US_REGULAR", "US_POST")
US_DAY: Final = "US_DAY"
#: The integrated Toss feed (hk 6322); the execution venue is unknown.
TOSS_US_VENUE: Final = "TOSS_INTEGRATED"
US_PRE_OPEN_ET: Final = time(4, 0)
US_POST_CLOSE_ET: Final = time(20, 0)
#: After-market close on an early-close day (Nasdaq ETA #2026-69, NYSE holidays
#: page: both extended sessions end at 5:00 p.m. ET when the regular session
#: closes at 13:00 ET).
US_EARLY_CLOSE_POST_CLOSE_ET: Final = time(17, 0)
_US_REGULAR_CLOSE_ET: Final = time(16, 0)

FUTURES_VENUE: Final = "KRX"
FUTURES_SESSIONS: Final = ("FUT_DAY", "FUT_NIGHT")
FUTURES_DAY: Final = (time(8, 45), time(15, 45))
#: Day-session close on the contract's last trading day (KRX 15:20).
FUTURES_LAST_TRADE_DAY_CLOSE: Final = time(15, 20)
FUTURES_NIGHT: Final = (time(18, 0), time(6, 0))
FUTURES_SYMBOL: Final = re.compile(r"^M?F200\.[0-9]{2}(?:0[1-9]|1[0-2])$")

#: Display order of the sessions inside one venue.
SESSION_ORDER: Final = (
    "KRX_REGULAR",
    "KRX_AFTER",
    "NXT_PRE",
    "NXT_OVERLAP",
    "NXT_POST",
    US_DAY,
    "US_PRE",
    "US_REGULAR",
    "US_POST",
    "FUT_DAY",
    "FUT_NIGHT",
)
_LOOKBACK_DAYS: Final = 15

Phase = Literal["before_open", "open", "ended", "not_trading_day", "unknown"]


@dataclass(frozen=True, slots=True)
class Window:
    """One session of one session date.

    ``start``/``end`` are ``None`` when the hours are not known (US day market:
    the broker calendar that defines them is not stored). ``earliest_start`` is
    then the first instant the session could begin, so "not started yet" is
    still certain.
    """

    start: datetime | None
    end: datetime | None
    earliest_start: datetime | None = None


UNKNOWN_WINDOW: Final = Window(None, None)


def _at(day: date, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)


def calendar_market(market: str) -> Market:
    """Exchange calendar of a market (KR futures follow the XKRX trading days)."""
    return "us" if market == "us" else "kr"


def day_status(market: str, day: date) -> SessionStatus:
    return trading_session_status(calendar_market(market), day)


def previous_session_date(market: str, day: date) -> date | None:
    """Last confirmed session before ``day``.

    ``None`` when the calendar cannot classify a day on the way back: skipping it
    could silently pick an older session.
    """
    cal = calendar_market(market)
    for offset in range(1, _LOOKBACK_DAYS + 1):
        candidate = day - timedelta(days=offset)
        status = trading_session_status(cal, candidate)
        if status == "open":
            return candidate
        if status == "unknown":
            return None
    return None


def kr_window(session: str, day: date) -> Window:
    for sessions in KR_SESSIONS.values():
        for name, start, end in sessions:
            if name == session:
                return Window(_at(day, start, _KST), _at(day, end, _KST))
    return UNKNOWN_WINDOW


def us_window(session: str, day: date) -> Window:
    """ET windows of the session date. The day market has no stored hours."""
    if session == US_DAY:
        earliest = _at(day - timedelta(days=1), US_POST_CLOSE_ET, _ET)
        return Window(None, None, earliest)
    bounds = regular_session_bounds("us", day)
    if bounds is None:
        return UNKNOWN_WINDOW
    opened, closed = (bound.astimezone(UTC) for bound in bounds)
    if session == "US_PRE":
        return Window(_at(day, US_PRE_OPEN_ET, _ET), opened)
    if session == "US_REGULAR":
        return Window(opened, closed)
    if session == "US_POST":
        return Window(closed, _at(day, us_post_close_et(closed), _ET))
    return UNKNOWN_WINDOW


def us_post_close_et(regular_close: datetime) -> time:
    """After-market end of that ET date: 17:00 on an early close, else 20:00."""
    if regular_close.astimezone(_ET).time() < _US_REGULAR_CLOSE_ET:
        return US_EARLY_CLOSE_POST_CLOSE_ET
    return US_POST_CLOSE_ET


def futures_window(session: str, day: date, symbol: str | None = None) -> Window:
    """Day session of ``day``, or the night session whose trading date is ``day``.

    The night session starts on the previous trading day and ends the next
    morning (hk 6148: ``session_date`` is the next trading day). ``symbol`` is
    the contract: its last trading day ends the day session at 15:20.
    """
    if session == "FUT_DAY":
        return Window(
            _at(day, FUTURES_DAY[0], _KST),
            _at(day, futures_day_close(symbol, day), _KST),
        )
    if session == "FUT_NIGHT":
        started = previous_session_date("futures", day)
        if started is None:
            return UNKNOWN_WINDOW
        start, end = FUTURES_NIGHT
        return Window(
            _at(started, start, _KST), _at(started + timedelta(days=1), end, _KST)
        )
    return UNKNOWN_WINDOW


def session_window(
    market: str, session: str, day: date, symbol: str | None = None
) -> Window:
    """Window of a session on a date the calendar confirms as a trading day.

    ``symbol`` names the futures contract whose last trading day shortens the
    day session; the other markets ignore it.
    """
    if market == "kr":
        return kr_window(session, day)
    if market == "us":
        return us_window(session, day)
    return futures_window(session, day, symbol)


def phase_of(window: Window, status: SessionStatus, as_of: datetime) -> Phase:
    """Where ``as_of`` stands against the session; the calendar comes first."""
    if status == "closed":
        return "not_trading_day"
    if status == "unknown":
        return "unknown"
    start = window.start or window.earliest_start
    if start is not None and as_of < start:
        return "before_open"
    if window.start is None or window.end is None:
        return "unknown"
    return "open" if as_of < window.end else "ended"


def second_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_thursday = first + timedelta(days=(3 - first.weekday()) % 7)
    return first_thursday + timedelta(days=7)


def contract_expired(symbol: str, day: date) -> bool:
    """True after the second Thursday of the contract month (``<PRODUCT>.<YYMM>``).

    Holiday shifts of the last trading day are not applied: this only decides
    whether a contract seen in the last days is still expected by default.
    """
    if not FUTURES_SYMBOL.fullmatch(symbol):
        return False
    yymm = symbol.rsplit(".", 1)[1]
    return day > second_thursday(2000 + int(yymm[:2]), int(yymm[2:]))


def contract_month_of(symbol: str) -> str | None:
    """``YYYY-MM`` of a contract symbol, or ``None`` when it is not one."""
    if not FUTURES_SYMBOL.fullmatch(symbol):
        return None
    yymm = symbol.rsplit(".", 1)[1]
    return f"20{yymm[:2]}-{yymm[2:]}"


def last_trade_date(contract_month: str) -> date | None:
    """Last trading day of a contract month (``YYYY-MM``); ``None`` if unknown.

    The second Thursday, moved earlier over a closed day (KRX specification).
    This is a copy of ``kis_futures_mock.session_calendar.last_trade_date``,
    which the retro/v1 import guard forbids; it reads the day from the shared
    exchange calendar instead of that module's hand-maintained table, and a
    test pins the two rules against each other.
    """
    year_text, _, month_text = contract_month.partition("-")
    candidate = second_thursday(int(year_text), int(month_text))
    for _ in range(7):
        status = trading_session_status("kr", candidate)
        if status == "unknown":
            return None
        if status == "open":
            return candidate
        candidate -= timedelta(days=1)
    return None


def futures_day_close(symbol: str | None, day: date) -> time:
    """Close of the futures day session of ``day`` for one contract.

    ``FUTURES_LAST_TRADE_DAY_CLOSE`` on the contract's last trading day,
    ``FUTURES_DAY[1]`` otherwise. A calendar that cannot confirm the last
    trading day keeps the regular close: not shortening the window can report a
    delay, never hide one.
    """
    month = contract_month_of(symbol) if symbol is not None else None
    if month is not None and last_trade_date(month) == day:
        return FUTURES_LAST_TRADE_DAY_CLOSE
    return FUTURES_DAY[1]
