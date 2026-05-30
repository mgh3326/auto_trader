"""Fail-closed XNYS / XKRX session+holiday calendar (ROB-371).

Wraps :mod:`exchange_calendars` (already a project dependency, used by
``invest_screener_snapshot_tasks`` and ``invest_screener_snapshots.freshness``).
``exchange_calendars`` is imported lazily so importing this module stays cheap;
the library memoizes ``get_calendar`` so repeated calls are amortized.

Fail-closed contract (ROB-367 §5 / ROB-371): any date the calendar cannot
positively classify as open — out of its precomputed range, or any
``ValueError``/``KeyError`` from the library or pandas (``OutOfBoundsDatetime``
is a ``ValueError``) — is treated as **not a trading session**. Lookahead-safe
labeling must never leak across a session it could not confirm is open.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Literal

logger = logging.getLogger(__name__)

Market = Literal["us", "kr"]

_CALENDAR_NAME: dict[str, str] = {"us": "XNYS", "kr": "XKRX"}

# Bounded search horizon for next/previous session lookups. The worst-case
# Korean lunar-holiday cluster (Seollal / Chuseok overlapping weekends and an
# adjacent public holiday) can span ~10 calendar days; the XNYS worst case is
# ~4 (Thanksgiving Wed -> following Mon). 32 leaves a generous safety margin so
# a real session is never missed within the supported calendar range.
_SESSION_SEARCH_DAYS = 32


def _calendar(market: Market):
    import exchange_calendars as xcals

    try:
        return xcals.get_calendar(_CALENDAR_NAME[market])
    except KeyError as exc:  # unknown market key -> programmer error, surface it
        raise ValueError(f"unsupported market {market!r}") from exc


def is_trading_session(market: Market, day: date) -> bool:
    """True iff ``day`` is a trading session on the market's exchange.

    Fail-closed: ANY error from the calendar load or query path returns
    ``False`` (never raises). Expected out-of-range / unrepresentable-date
    errors (``ValueError``/``KeyError``) are swallowed silently; genuinely
    unexpected errors are logged (so library/data bugs aren't hidden) but still
    fail closed — the contract is that we never claim a session we can't confirm.
    """
    import pandas as pd

    try:
        cal = _calendar(market)
        return bool(cal.is_session(pd.Timestamp(day)))
    except (ValueError, KeyError):
        return False
    except Exception:  # noqa: BLE001 - fail-closed on any calendar/library error
        logger.warning(
            "session_calendar: unexpected error classifying %s %s; treating as "
            "closed (fail-closed)",
            market,
            day,
            exc_info=True,
        )
        return False


def next_trading_session(market: Market, day: date) -> date | None:
    """First trading session strictly after ``day`` within a bounded horizon.

    Returns ``None`` if none can be confirmed (fail-closed / out of range).
    """
    for offset in range(1, _SESSION_SEARCH_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if is_trading_session(market, candidate):
            return candidate
    return None


def previous_trading_session(market: Market, day: date) -> date | None:
    """Last trading session strictly before ``day`` within a bounded horizon."""
    for offset in range(1, _SESSION_SEARCH_DAYS + 1):
        candidate = day - timedelta(days=offset)
        if is_trading_session(market, candidate):
            return candidate
    return None


def trading_sessions_in_range(market: Market, start: date, end: date) -> list[date]:
    """Trading-session dates in the inclusive ``[start, end]`` range.

    Empty when ``end < start`` or the range is out of the calendar's bounds
    (fail-closed).
    """
    import pandas as pd

    if end < start:
        return []
    try:
        cal = _calendar(market)
        sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    except (ValueError, KeyError):
        return []
    except Exception:  # noqa: BLE001 - fail-closed on any calendar/library error
        logger.warning(
            "session_calendar: unexpected error enumerating %s %s..%s; treating "
            "as no sessions (fail-closed)",
            market,
            start,
            end,
            exc_info=True,
        )
        return []
    return [ts.date() for ts in sessions]
