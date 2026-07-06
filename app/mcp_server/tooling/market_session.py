"""ROB-464: KR market-session awareness for MCP read tools.

When the KRX regular session is closed (pre-market, after-hours, weekend,
holiday) the KIS-backed quote/index/ranking tools otherwise surface the prior
close as if it were live: ``price == previous_close``, ``change_pct == 0``, and
all-zero ``get_top_stocks`` rankings sorted alphabetically. These helpers let the
read tools tag a ``data_state`` and suppress fake-zero values instead of
presenting stale data as current.

The classification is backed by ``exchange_calendars`` (``XKRX``), so KR holidays
and the weekend are handled correctly — the same primitive the watch scanners use
via :func:`app.jobs.watch_market_data.is_market_open`.
"""

from __future__ import annotations

import datetime as _dt
from datetime import time as _time
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.market_events.session_calendar import regular_session_bounds

# data_state values surfaced to MCP callers.
DATA_STATE_FRESH = "fresh"
DATA_STATE_STALE = "stale"
DATA_STATE_PREMARKET_UNAVAILABLE = "premarket_unavailable"
DATA_STATE_MARKET_CLOSED = "market_closed"

US_SESSION_PREMARKET = "premarket"
US_SESSION_REGULAR = "regular"
US_SESSION_AFTERHOURS = "afterhours"
US_SESSION_CLOSED = "closed"

_UTC = ZoneInfo("UTC")
_ET = ZoneInfo("America/New_York")
_US_PRE_OPEN = _time(4, 0)
_US_AFTER_CLOSE = _time(20, 0)

# XKRX regular session opens at 09:00 KST.
_KR_OPEN = pd.Timestamp("2000-01-01 09:00").time()


@lru_cache(maxsize=1)
def _get_kr_calendar() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XKRX")


def kr_market_data_state(now: Any = None) -> str:
    """Classify the freshness of KRX regular-session market data right now.

    Returns one of:

    - ``DATA_STATE_FRESH`` — XKRX regular session is trading → data is live.
    - ``DATA_STATE_PREMARKET_UNAVAILABLE`` — a KRX trading day, before the
      session opens (09:00 KST). NXT may be trading, but the KRX-backed tools
      only return the prior close, so the value is not yet live.
    - ``DATA_STATE_MARKET_CLOSED`` — after close, weekend, or holiday.

    ``now`` accepts any pandas-parseable timestamp (defaults to current UTC);
    naive timestamps are assumed UTC.
    """
    cal = _get_kr_calendar()
    ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now("UTC")
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    local = ts.tz_convert(cal.tz)

    if cal.is_trading_minute(local.floor("min")):
        return DATA_STATE_FRESH

    session_day = pd.Timestamp(local.date())
    if cal.is_session(session_day) and local.time() < _KR_OPEN:
        return DATA_STATE_PREMARKET_UNAVAILABLE
    return DATA_STATE_MARKET_CLOSED


def us_market_session(now: Any = None) -> str:
    """Classify the current US equity quote session using XNYS regular bounds.

    Returns ``premarket`` for 04:00 ET up to the XNYS open, ``regular`` for
    XNYS regular hours, ``afterhours`` from the XNYS close up to 20:00 ET, and
    ``closed`` outside those windows or on non-session days. Naive timestamps
    are treated as UTC. Early closes are honored by ``regular_session_bounds``.
    """
    current = now if now is not None else _dt.datetime.now(_dt.UTC)
    if not isinstance(current, _dt.datetime):
        current = pd.Timestamp(current).to_pydatetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC)

    local = current.astimezone(_ET)
    bounds = regular_session_bounds("us", local.date())
    if bounds is None:
        return US_SESSION_CLOSED

    open_utc, close_utc = bounds
    current_utc = current.astimezone(_UTC)
    if open_utc <= current_utc < close_utc:
        return US_SESSION_REGULAR

    open_local = open_utc.astimezone(_ET)
    close_local = close_utc.astimezone(_ET)
    pre_open = local.replace(
        hour=_US_PRE_OPEN.hour, minute=0, second=0, microsecond=0
    )
    after_close = local.replace(
        hour=_US_AFTER_CLOSE.hour, minute=0, second=0, microsecond=0
    )
    if pre_open <= local < open_local:
        return US_SESSION_PREMARKET
    if close_local <= local < after_close:
        return US_SESSION_AFTERHOURS
    return US_SESSION_CLOSED


def is_kr_session_day(date: Any) -> bool:
    """True when ``date`` (a KST calendar date) is an XKRX trading session."""
    return bool(_get_kr_calendar().is_session(pd.Timestamp(date)))


def previous_kr_session(date: Any) -> _dt.date:
    """Return the XKRX trading session strictly before ``date``.

    ``date`` is a KST calendar date and need not itself be a session — for a
    weekend or holiday input the most recent prior session is returned. The
    result is always strictly earlier than ``date``, so passing a session day
    yields the session before it (not the same day). This correctly handles the
    Monday-after-holiday edge: e.g. with 2026-06-06 (현충일) on a Saturday, the
    session before Monday 2026-06-08 is Friday 2026-06-05, and after a multi-day
    holiday (Lunar New Year) it walks back to the last session before it.

    Backed by the XKRX calendar's ``date_to_session(..., direction="previous")``
    applied to the day before ``date`` so the result is never on-or-after
    ``date``.
    """
    cal = _get_kr_calendar()
    target = pd.Timestamp(date).normalize() - pd.Timedelta(days=1)
    session = cal.date_to_session(target, direction="previous")
    return pd.Timestamp(session).date()
