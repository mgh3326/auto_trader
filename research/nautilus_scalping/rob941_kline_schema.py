"""ROB-941 (AC4/AC5) — normalized 1m kline schema + fail-closed validation.

Real Binance USD-M kline CSV columns (confirmed via a live probe of
``BTCUSDT-1m-2025-07.csv``, header present in current archives):

    open_time,open,high,low,close,volume,close_time,quote_volume,count,
    taker_buy_volume,taker_buy_quote_volume,ignore

Every real row observed has ``close_time - open_time == 59999`` and
``open_time`` aligned to the 60000ms UTC grid. This module fails closed (raises)
on: malformed columns, non-positive/inconsistent OHLC, negative volume/trade
count, a taker-buy volume exceeding total volume, off-grid ``open_time``, and a
bar duration other than exactly 59999ms. Conflicting duplicate ``open_time``
rows (same timestamp, different content) also fail closed; byte-identical
duplicates are deduped. Rows outside the caller's ``[window_start_ms,
window_end_ms)`` are silently clipped (not a gap — just out of scope), never
deleted/forward-filled/synthesized for rows INSIDE the window.

AC5 footnote (R1 M1, not changed by design): "unterminated bars are excluded"
is implemented here as a fail-closed ``raise`` (``InvalidOHLCVError`` on a
duration != 59999ms), not a silent skip. This is strictly MORE conservative
than exclude, and never actually fires on real data in this corpus: Binance's
monthly historical archives only ever contain fully-closed bars, and the
frozen half-open window (``rob941_frozen_scope``) already excludes the
in-progress "current" bar by construction. The raise exists as a fail-closed
guard against a corrupt/truncated archive, not as the exclude mechanism AC5
describes for a live/streaming context.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

GRID_MS = 60_000
_EXPECTED_DURATION_MS = (
    GRID_MS - 1
)  # 59999ms, Binance convention (close_time is inclusive-end - 1ms)
_HEADER_TOKEN = "open_time"
_EPS = 1e-6


class InvalidOHLCVError(ValueError):
    """A row's fields are structurally/economically impossible, or its bar duration
    or grid alignment is wrong — fail-closed, never coerced or dropped silently."""


class ConflictingDuplicateError(ValueError):
    """Two rows share ``open_time_ms`` but disagree on content — fail-closed."""


@dataclass(frozen=True)
class NormalizedKline:
    symbol: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    close_time_ms: int
    quote_volume: float
    trade_count: int
    taker_buy_volume: float
    taker_buy_quote_volume: float


def parse_kline_row(symbol: str, fields: list[str]) -> NormalizedKline:
    if len(fields) < 11:
        raise InvalidOHLCVError(
            f"{symbol}: expected >=11 kline columns, got {len(fields)}: {fields!r}"
        )

    open_time_ms = int(fields[0])
    open_ = float(fields[1])
    high = float(fields[2])
    low = float(fields[3])
    close = float(fields[4])
    base_volume = float(fields[5])
    close_time_ms = int(fields[6])
    quote_volume = float(fields[7])
    trade_count = int(float(fields[8]))
    taker_buy_volume = float(fields[9])
    taker_buy_quote_volume = float(fields[10])

    if open_time_ms % GRID_MS != 0:
        raise InvalidOHLCVError(
            f"{symbol}@{open_time_ms}: open_time_ms is not 60s-grid aligned"
        )
    duration_ms = close_time_ms - open_time_ms
    if duration_ms != _EXPECTED_DURATION_MS:
        raise InvalidOHLCVError(
            f"{symbol}@{open_time_ms}: bar duration {duration_ms}ms != expected "
            f"{_EXPECTED_DURATION_MS}ms — corrupt or unterminated bar"
        )
    if not (open_ > 0 and high > 0 and low > 0 and close > 0):
        raise InvalidOHLCVError(f"{symbol}@{open_time_ms}: non-positive OHLC price")
    if high < low:
        raise InvalidOHLCVError(f"{symbol}@{open_time_ms}: high < low")
    if high < open_ or high < close:
        raise InvalidOHLCVError(f"{symbol}@{open_time_ms}: high below open/close")
    if low > open_ or low > close:
        raise InvalidOHLCVError(f"{symbol}@{open_time_ms}: low above open/close")
    if base_volume < 0 or quote_volume < 0 or trade_count < 0:
        raise InvalidOHLCVError(f"{symbol}@{open_time_ms}: negative volume/trade_count")
    if taker_buy_volume < -_EPS or taker_buy_volume > base_volume + _EPS:
        raise InvalidOHLCVError(
            f"{symbol}@{open_time_ms}: taker_buy_volume out of [0, base_volume]"
        )
    if taker_buy_quote_volume < -_EPS or taker_buy_quote_volume > quote_volume + _EPS:
        raise InvalidOHLCVError(
            f"{symbol}@{open_time_ms}: taker_buy_quote_volume out of [0, quote_volume]"
        )

    return NormalizedKline(
        symbol=symbol,
        open_time_ms=open_time_ms,
        open=open_,
        high=high,
        low=low,
        close=close,
        base_volume=base_volume,
        close_time_ms=close_time_ms,
        quote_volume=quote_volume,
        trade_count=trade_count,
        taker_buy_volume=taker_buy_volume,
        taker_buy_quote_volume=taker_buy_quote_volume,
    )


def parse_kline_csv(
    symbol: str, text: str, window_start_ms: int, window_end_ms: int
) -> list[NormalizedKline]:
    """Parse a decompressed monthly kline CSV into sorted, deduped, window-clipped rows.

    Rows outside ``[window_start_ms, window_end_ms)`` are clipped silently (out of
    the frozen corpus scope). Conflicting duplicate ``open_time_ms`` rows raise;
    byte-identical duplicates are deduped.
    """
    seen: dict[int, NormalizedKline] = {}
    order: list[int] = []
    for row_fields in csv.reader(io.StringIO(text)):
        if not row_fields:
            continue
        if row_fields[0].strip().lower() == _HEADER_TOKEN:
            continue
        row = parse_kline_row(symbol, row_fields)
        if row.open_time_ms < window_start_ms or row.open_time_ms >= window_end_ms:
            continue
        existing = seen.get(row.open_time_ms)
        if existing is not None:
            if existing != row:
                raise ConflictingDuplicateError(
                    f"{symbol}@{row.open_time_ms}: conflicting duplicate rows"
                )
            continue  # identical duplicate -> dedupe
        seen[row.open_time_ms] = row
        order.append(row.open_time_ms)
    return [seen[t] for t in sorted(order)]
