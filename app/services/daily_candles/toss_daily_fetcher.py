"""Toss-side fetcher facade for the daily candle store.

Wraps the Toss candle endpoint for daily (1d interval) KR equity data
so the DailyCandleSyncService can use it as a primary or fallback source.
This module knows about the Toss broker; it does NOT know about the database.
Returns canonical pandas DataFrames with columns: date, open, high, low, close,
volume, value.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.services.brokers.toss.candles import fetch_toss_candles_frame
from app.services.brokers.toss.dto import TossCandlesPage

_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


class _TossDailyClient(Protocol):
    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage: ...


def _to_daily_frame(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """Convert candles frame (has datetime col) to daily frame (has date col)."""
    if raw_frame.empty:
        return pd.DataFrame(columns=_FRAME_COLUMNS)
    frame = raw_frame.copy()
    if "date" not in frame.columns and "datetime" in frame.columns:
        frame["date"] = pd.to_datetime(frame["datetime"]).dt.date
    return frame[_FRAME_COLUMNS].sort_values("date").reset_index(drop=True)


async def fetch_kr_daily_toss(
    *,
    client: _TossDailyClient,
    symbol: str,
    n: int,
    max_pages: int = 50,
) -> pd.DataFrame:
    """Fetch n daily bars for a KR equity symbol from Toss.

    Uses the Toss 1d interval endpoint with split-adjusted prices.
    Returns a DataFrame with columns: date, open, high, low, close, volume, value.
    """
    raw = await fetch_toss_candles_frame(
        client=client,
        symbol=symbol,
        interval="1d",
        count=n,
        adjusted=True,
        max_pages=max_pages,
    )
    return _to_daily_frame(raw)


async def fetch_daily_toss_unclamped(*, symbol: str, n: int) -> pd.DataFrame:
    """Fetch daily candles from Toss using a settings-configured client."""
    from app.services.brokers.toss.client import TossReadClient
    client = TossReadClient.from_settings()
    try:
        return await fetch_kr_daily_toss(client=client, symbol=symbol, n=n)
    finally:
        await client.aclose()

