from __future__ import annotations

import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

_KST = ZoneInfo("Asia/Seoul")
_KRX_PREOPEN_OFFSET = datetime.timedelta(hours=1)
_KRX_POSTCLOSE_OFFSET = datetime.timedelta(hours=4, minutes=30)

_ROUTE_TO_EXCHANGE = {
    "J": "KRX",
    "NX": "NXT",
}
_EXCHANGE_TO_ROUTE = {
    "KRX": "J",
    "NXT": "NX",
}


def normalize_route(route: str) -> str:
    normalized = str(route or "").strip().upper()
    if normalized not in _ROUTE_TO_EXCHANGE:
        raise ValueError(f"Unsupported KR route: {route}")
    return normalized


def normalize_exchange(exchange: str) -> str:
    normalized = str(exchange or "").strip().upper()
    if normalized not in _EXCHANGE_TO_ROUTE:
        raise ValueError(f"Unsupported KR exchange: {exchange}")
    return normalized


def exchange_for_route(route: str) -> str:
    return _ROUTE_TO_EXCHANGE[normalize_route(route)]


def route_for_exchange(exchange: str) -> str:
    return _EXCHANGE_TO_ROUTE[normalize_exchange(exchange)]


@lru_cache(maxsize=1)
def get_xkrx_calendar():
    return xcals.get_calendar("XKRX")


def _to_kst_timestamp(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(_KST)


def get_session_bounds(
    route: str,
    day: datetime.date,
) -> tuple[datetime.datetime, datetime.datetime] | None:
    normalized_route = normalize_route(route)
    calendar = get_xkrx_calendar()

    session_label = pd.Timestamp(day).normalize().tz_localize(None)
    schedule = calendar.schedule
    if session_label not in schedule.index:
        return None

    row = schedule.loc[session_label]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]

    open_value = row.get("market_open", row.get("open"))
    close_value = row.get("market_close", row.get("close"))
    if open_value is None or close_value is None:
        return None

    open_kst = _to_kst_timestamp(open_value).to_pydatetime()
    close_kst = _to_kst_timestamp(close_value).to_pydatetime()
    if normalized_route == "NX":
        return (
            open_kst - _KRX_PREOPEN_OFFSET,
            close_kst + _KRX_POSTCLOSE_OFFSET,
        )
    return open_kst, close_kst


__all__ = [
    "exchange_for_route",
    "get_session_bounds",
    "get_xkrx_calendar",
    "normalize_exchange",
    "normalize_route",
    "route_for_exchange",
]
