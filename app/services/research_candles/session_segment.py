"""hk 6145: session label of a 1-minute bar, from its start time.

One pure function, market-generic (hk 6148 reuses it for futures):

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


def session_segment_of(market: str, venue: str, bar_start_utc: datetime) -> str:
    """Session label of the bar that starts at ``bar_start_utc`` (tz-aware).

    ``market`` is ``"kr"`` or ``"us"``; anything else, a naive timestamp or an
    unknown venue gives ``UNKNOWN``.
    """
    if bar_start_utc.tzinfo is None:
        return UNKNOWN
    bar_start = bar_start_utc.astimezone(UTC)
    if market == "kr":
        return _kr_segment(venue, bar_start)
    if market == "us":
        return _us_segment(venue, bar_start)
    return UNKNOWN
