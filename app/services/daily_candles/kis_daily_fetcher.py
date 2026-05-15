"""KIS-side fetcher facade for the daily candle store.

Wraps the unclamped KIS daily endpoints so the sync service and backfill
CLI can request horizons greater than the wrapper-level display safety
clamp (DEFAULT_CANDLES=200). This module knows about KIS; it does NOT
know about the database. It returns canonical pandas DataFrames.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

import pandas as pd


class _KISDailyClient(Protocol):
    async def inquire_daily_itemchartprice_unclamped(
        self,
        *,
        code: str,
        market: str,
        n: int,
        period: str,
        end_date: dt.date | None,
    ) -> pd.DataFrame: ...

    async def inquire_overseas_daily_price_unclamped(
        self,
        *,
        symbol: str,
        exchange_code: str,
        n: int,
        period: str,
    ) -> pd.DataFrame: ...


async def fetch_kr_daily_unclamped(
    *,
    kis: _KISDailyClient,
    code: str,
    n: int,
    market: str = "J",
    period: str = "D",
    end_date: dt.date | None = None,
) -> pd.DataFrame:
    return await kis.inquire_daily_itemchartprice_unclamped(
        code=code, market=market, n=n, period=period, end_date=end_date
    )


async def fetch_us_daily_unclamped(
    *,
    kis: _KISDailyClient,
    symbol: str,
    exchange_code: str,
    n: int,
    period: str = "D",
) -> pd.DataFrame:
    return await kis.inquire_overseas_daily_price_unclamped(
        symbol=symbol, exchange_code=exchange_code, n=n, period=period
    )
