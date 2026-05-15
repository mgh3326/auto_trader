"""Yahoo Finance fallback fetcher for US daily candles.

Used by the daily candle sync when KIS overseas daily returns empty
for a specific symbol (illiquid names, ETF gaps), and optionally as an
adj_close enrichment source. This module knows about Yahoo; it does
NOT know about the database or about KIS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

import app.services.brokers.yahoo.client as yahoo_service


@dataclass(frozen=True, slots=True)
class YahooFallbackRow:
    time_utc: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float
    value: float


async def fetch_us_daily_yahoo_fallback(
    *, symbol: str, n: int
) -> list[YahooFallbackRow]:
    frame = await yahoo_service.fetch_ohlcv(ticker=symbol, days=n, period="day")
    if frame.empty or "close" not in frame.columns:
        return []

    has_adj = "adj_close" in frame.columns
    out: list[YahooFallbackRow] = []
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        if raw_date is None:
            continue
        ts = pd.Timestamp(raw_date)
        if ts.tzinfo is None:
            ts = ts.tz_localize(UTC)
        else:
            ts = ts.tz_convert(UTC)
        close = float(record["close"])
        volume = float(record.get("volume") or 0.0)
        out.append(
            YahooFallbackRow(
                time_utc=ts.to_pydatetime(),
                symbol=symbol,
                open=float(record["open"]) if record.get("open") is not None else close,
                high=float(record["high"]) if record.get("high") is not None else close,
                low=float(record["low"]) if record.get("low") is not None else close,
                close=close,
                adj_close=(
                    float(record["adj_close"])
                    if has_adj and record.get("adj_close") is not None
                    else None
                ),
                volume=volume,
                value=close * volume,
            )
        )
    return out
