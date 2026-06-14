from __future__ import annotations

import datetime as dt

import pandas as pd

from app.services.brokers.kis._base_market_data import _aggregate_minute_candles_frame
from app.services.brokers.toss.candles import fetch_toss_candles_frame
from app.services.brokers.toss.client import TossReadClient

_KR_INTRADAY_BUCKET_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def _before_from_end_date(end_date: dt.datetime | None) -> str | None:
    if end_date is None:
        return None
    return end_date.isoformat()


async def fetch_kr_intraday_toss_frame(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None,
) -> pd.DataFrame:
    bucket = _KR_INTRADAY_BUCKET_MINUTES[period]
    # ROB-548: aggregating N buckets needs N*bucket one-minute candles. Page
    # through them (200/page) instead of the old single-page 200 hard cap that
    # silently truncated 5m/15m/30m/1h to ~40/13/6/3 rows.
    request_count = count if bucket == 1 else max(count * bucket, bucket)
    client = TossReadClient.from_settings()
    try:
        one_minute = await fetch_toss_candles_frame(
            client=client,
            symbol=symbol,
            interval="1m",
            count=request_count,
            before=_before_from_end_date(end_date),
            max_pages=max(1, (request_count + 199) // 200),
        )
    finally:
        await client.aclose()
    if bucket == 1:
        return one_minute.tail(count).reset_index(drop=True)
    aggregated = _aggregate_minute_candles_frame(
        one_minute,
        bucket,
        include_partial=(bucket == 60),
    )
    return aggregated.tail(count).reset_index(drop=True)


async def fetch_daily_toss_frame(
    *,
    symbol: str,
    count: int,
    end_date: dt.datetime | None = None,
) -> pd.DataFrame:
    client = TossReadClient.from_settings()
    try:
        return await fetch_toss_candles_frame(
            client=client,
            symbol=symbol,
            interval="1d",
            count=count,
            before=_before_from_end_date(end_date),
            adjusted=True,
            max_pages=max(1, (count + 199) // 200),
        )
    finally:
        await client.aclose()
