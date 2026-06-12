from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.services.brokers.toss.dto import TossCandlesPage

_FRAME_COLUMNS = ["datetime", "date", "time", "open", "high", "low", "close", "volume", "value"]


class _TossCandleClient(Protocol):
    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> TossCandlesPage: ...


def empty_toss_candles_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_FRAME_COLUMNS)


def toss_candles_page_to_frame(page: TossCandlesPage) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for candle in page.candles:
        timestamp = pd.Timestamp(candle.timestamp)
        close = float(candle.close_price)
        volume = float(candle.volume)
        records.append(
            {
                "datetime": timestamp,
                "date": timestamp.date(),
                "time": timestamp.time(),
                "open": float(candle.open_price),
                "high": float(candle.high_price),
                "low": float(candle.low_price),
                "close": close,
                "volume": volume,
                "value": close * volume,
            }
        )
    if not records:
        return empty_toss_candles_frame()
    return pd.DataFrame(records).sort_values("datetime").reset_index(drop=True).loc[:, _FRAME_COLUMNS]


async def fetch_toss_candles_frame(
    *,
    client: _TossCandleClient,
    symbol: str,
    interval: str,
    count: int,
    before: str | None = None,
    adjusted: bool | None = None,
    max_pages: int = 20,
) -> pd.DataFrame:
    remaining = max(int(count), 1)
    cursor = before
    frames: list[pd.DataFrame] = []
    for _ in range(max_pages):
        page_count = min(remaining, 200)
        page = await client.candles(
            symbol,
            interval=interval,
            count=page_count,
            before=cursor,
            adjusted=adjusted,
        )
        frame = toss_candles_page_to_frame(page)
        if not frame.empty:
            frames.append(frame)
            remaining -= len(frame)
        if remaining <= 0 or not page.next_before:
            break
        cursor = page.next_before
    if not frames:
        return empty_toss_candles_frame()
    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["datetime"])
    return combined.sort_values("datetime").tail(count).reset_index(drop=True).loc[:, _FRAME_COLUMNS]
