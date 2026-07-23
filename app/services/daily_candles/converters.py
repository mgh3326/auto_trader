"""Pure DataFrame -> repository-row converters for the daily candle store.

Used by both the sync service (orchestrating ingestion from external
fetchers) and the cache-first read path in market_data_indicators. The
function is intentionally minimal: it does not know about DB or
external APIs.
"""

from __future__ import annotations

from datetime import UTC

import pandas as pd

from app.services.daily_candles.repository import DailyCandleRow


def frame_to_rows(
    frame: pd.DataFrame,
    *,
    symbol: str,
    partition: str,
    source: str,
) -> list[DailyCandleRow]:
    """Convert a pandas DataFrame with date/OHLCV columns to ``DailyCandleRow``s.

    The DataFrame is expected to have a ``date`` (or ``datetime``) column and
    ``close`` column at minimum. Missing OHLC values default to ``close``. A
    missing ``value`` column is computed as ``close * volume``. Times are
    normalized to UTC.

    Empty frames or frames without a ``close`` column return ``[]``.
    """
    if frame is None or frame.empty or "close" not in frame.columns:
        return []

    out: list[DailyCandleRow] = []
    for record in frame.to_dict("records"):
        raw_date = record.get("date")
        if raw_date is None:
            raw_date = record.get("datetime")
        if raw_date is None:
            continue
        ts = pd.Timestamp(raw_date)
        if ts.tzinfo is None:
            ts = ts.tz_localize(UTC)
        else:
            ts = ts.tz_convert(UTC)

        close = float(record["close"])
        # Explicit None check (not truthiness) preserves legitimate 0.0 values.
        volume = float(record["volume"]) if record.get("volume") is not None else 0.0
        open_value = float(record["open"]) if record.get("open") is not None else close
        high_value = float(record["high"]) if record.get("high") is not None else close
        low_value = float(record["low"]) if record.get("low") is not None else close
        raw_value = record.get("value")
        computed_value = float(raw_value) if raw_value is not None else close * volume

        adj_close_raw = record.get("adj_close")
        adj_close: float | None = (
            float(adj_close_raw) if adj_close_raw is not None else None
        )

        out.append(
            DailyCandleRow(
                time_utc=ts.to_pydatetime(),
                symbol=symbol,
                partition=partition,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close,
                adj_close=adj_close,
                volume=volume,
                value=computed_value,
                source=source,
            )
        )
    return out
