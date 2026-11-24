"""hk 6145: session label of a 1-minute bar, from its start time.

One pure function, market-generic (hk 6148 adds futures):

    session_segment_of(market, venue, bar_start_utc) -> str

The windows are not re-typed here. KR edges come from
``app.services.brokers.kis.live_order_expiry`` (the #925 / ROB-671 definitions)
and US edges from ``app.mcp_server.tooling.market_session.us_market_session``
(XNYS regular bounds incl. early closes, 04:00-20:00 ET extended window). The
trading-day check is ``app.services.market_events.session_calendar`` and is
fail-closed: a day the calendar cannot confirm is ``UNKNOWN``.

Windows are start-inclusive / end-exclusive on the bar START time:

KR (KST), only on a confirmed XKRX trading day:
  KRX  09:00-15:30 KRX_REGULAR; 16:00-20:00 KRX_AFTER
  NTX  08:00-08:50 NXT_PRE; 09:00-15:30 NXT_OVERLAP; 15:30-20:00 NXT_POST
  everything else (incl. the 08:50-09:00 gap and KRX 15:30-16:00) -> UNKNOWN
US (ET): 04:00-regular open US_PRE; regular session US_REGULAR;
  regular close-20:00 US_POST; 20:00-04:00 and non-session days -> UNKNOWN
  (``US_DAY`` exists in the table CHECK for a later day-market reader; this
  function never returns it).

Futures (hk 6148, ``market="futures"``, venue ignored — one KRX derivatives venue),
KST on the bar START, ``futures_session_date`` gives the trading date:
  08:45-15:45 FUT_DAY on a confirmed XKRX trading day; session_date = that day
  18:00-06:00 FUT_NIGHT: the night session that STARTED on KST day D (bars before
  06:00 belong to the previous calendar day's night); D must be an XKRX trading
  day, and session_date is the NEXT XKRX trading day after D (Friday night ->
  Monday; the night before a holiday -> the first trading day after it)
  everything else, a non-trading D, or a calendar that cannot confirm the next
  trading day -> UNKNOWN with session_date None (never guessed).

``UNKNOWN`` is a value, not an error: it is stored, never guessed into a
concrete session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.mcp_server.tooling.market_session import (
    US_EXTENDED_CLOSE_ET,
    US_SESSION_AFTERHOURS,
    US_SESSION_PREMARKET,
    US_SESSION_REGULAR,
    us_market_session,
)
from app.services.brokers.kis.live_order_expiry import (
    KR_PREMARKET_CLOSE_KST,
    KR_PREMARKET_OPEN_KST,
    KR_REGULAR_CLOSE_KST,
    KR_REGULAR_OPEN_KST,
    KRX_AFTER_CLOSE_KST,
    KRX_AFTER_OPEN_KST,
)
from app.services.kis_futures_mock.session_calendar import (
    REGULAR_CLOSE as FUTURES_DAY_CLOSE_KST,
)
from app.services.kis_futures_mock.session_calendar import (
    REGULAR_OPEN as FUTURES_DAY_OPEN_KST,
)
from app.services.market_events.session_calendar import is_trading_session

UNKNOWN = "UNKNOWN"

KRX_REGULAR = "KRX_REGULAR"
KRX_AFTER = "KRX_AFTER"
NXT_PRE = "NXT_PRE"
NXT_OVERLAP = "NXT_OVERLAP"
NXT_POST = "NXT_POST"
US_PRE = "US_PRE"
US_REGULAR = "US_REGULAR"
US_POST = "US_POST"
FUT_DAY = "FUT_DAY"
FUT_NIGHT = "FUT_NIGHT"

FUTURES_NIGHT_OPEN_KST = time(18, 0)
FUTURES_NIGHT_CLOSE_KST = time(6, 0)
#: Calendar days searched for the next trading day after a night session start.
_FUTURES_NEXT_DAY_SEARCH = 10

KR_VENUES = ("KRX", "NTX")
US_VENUES = ("NASD", "NYSE", "AMEX")

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

_US_SEGMENT_BY_SESSION = {
    US_SESSION_PREMARKET: US_PRE,
    US_SESSION_REGULAR: US_REGULAR,
    US_SESSION_AFTERHOURS: US_POST,
}


@lru_cache(maxsize=512)
def _kr_session_day(day: date) -> bool:
    return is_trading_session("kr", day)


def _in(clock: time, start: time, end: time) -> bool:
    return start <= clock < end


def _kr_segment(venue: str, bar_start_utc: datetime) -> str:
    local = bar_start_utc.astimezone(_KST)
    if not _kr_session_day(local.date()):
        return UNKNOWN
    clock = local.time()
    if venue == "KRX":
        if _in(clock, KR_REGULAR_OPEN_KST, KR_REGULAR_CLOSE_KST):
            return KRX_REGULAR
        if _in(clock, KRX_AFTER_OPEN_KST, KRX_AFTER_CLOSE_KST):
            return KRX_AFTER
        return UNKNOWN
    if venue == "NTX":
        if _in(clock, KR_PREMARKET_OPEN_KST, KR_PREMARKET_CLOSE_KST):
            return NXT_PRE
        if _in(clock, KR_REGULAR_OPEN_KST, KR_REGULAR_CLOSE_KST):
            return NXT_OVERLAP
        if _in(clock, KR_REGULAR_CLOSE_KST, KRX_AFTER_CLOSE_KST):
            return NXT_POST
    return UNKNOWN


def _us_segment(venue: str, bar_start_utc: datetime) -> str:
    if venue not in US_VENUES:
        return UNKNOWN
    return _US_SEGMENT_BY_SESSION.get(us_market_session(bar_start_utc), UNKNOWN)


def us_session_date(bar_start_utc: datetime) -> date:
    """ET trading day of an instant: rolls over at 20:00 ET (DST aware).

    Same rule as ``execution_ledger.kis_lots.us_trading_day_window`` (a bar in
    the 04:00-20:00 ET extended session belongs to its own ET date, a daytime
    bar after 20:00 to the next one), kept here as a pure helper so research
    code does not import the ledger package.
    """
    local = bar_start_utc.astimezone(_ET)
    if local.time() >= US_EXTENDED_CLOSE_ET:
        return local.date() + timedelta(days=1)
    return local.date()


@lru_cache(maxsize=512)
def _next_kr_trading_day(day: date) -> date | None:
    for offset in range(1, _FUTURES_NEXT_DAY_SEARCH + 1):
        candidate = day + timedelta(days=offset)
        if _kr_session_day(candidate):
            return candidate
    return None


def _futures_classify(bar_start_utc: datetime) -> tuple[str, date | None]:
    local = bar_start_utc.astimezone(_KST)
    clock = local.time()
    if _in(clock, FUTURES_DAY_OPEN_KST, FUTURES_DAY_CLOSE_KST):
        if _kr_session_day(local.date()):
            return FUT_DAY, local.date()
        return UNKNOWN, None
    if clock >= FUTURES_NIGHT_OPEN_KST or clock < FUTURES_NIGHT_CLOSE_KST:
        start_day = (
            local.date()
            if clock >= FUTURES_NIGHT_OPEN_KST
            else local.date() - timedelta(days=1)
        )
        if not _kr_session_day(start_day):
            return UNKNOWN, None
        trading_date = _next_kr_trading_day(start_day)
        if trading_date is None:
            return UNKNOWN, None
        return FUT_NIGHT, trading_date
    return UNKNOWN, None


def futures_session_date(bar_start_utc: datetime) -> date | None:
    """Trading date of a futures bar (day bar: its KST date; night bar: the next
    XKRX trading day after the night session start). ``None`` when the segment is
    ``UNKNOWN`` or the timestamp is naive."""
    if bar_start_utc.tzinfo is None:
        return None
    return _futures_classify(bar_start_utc.astimezone(UTC))[1]


def session_segment_of(market: str, venue: str, bar_start_utc: datetime) -> str:
    """Session label of the bar that starts at ``bar_start_utc`` (tz-aware).

    ``market`` is ``"kr"``, ``"us"`` or ``"futures"``; anything else, a naive
    timestamp or an unknown venue gives ``UNKNOWN``.
    """
    if bar_start_utc.tzinfo is None:
        return UNKNOWN
    bar_start = bar_start_utc.astimezone(UTC)
    if market == "kr":
        return _kr_segment(venue, bar_start)
    if market == "us":
        return _us_segment(venue, bar_start)
    if market == "futures":
        return _futures_classify(bar_start)[0]
    return UNKNOWN
