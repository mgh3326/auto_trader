"""Bounded reads of the ROB-993 complete-only 1m -> 4h collector for the trend lane.

Signals need only the 4h closes named by :func:`required_close_times`; each is
built from the 240 one-minute rows of its own bucket, so a bucket missing any
minute simply yields no bar (NO_SIGNAL) and nothing is forward-filled. The
hard-stop watch reads a bounded 1m window since the fill.

Transport is the unsigned public kline reader already pinned to the Futures
Demo host; ``transport`` is whatever exposes ``await get(path, params=...)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal

from app.services.brokers.binance.demo_strategy_loop import bars as rob993_bars
from research.nautilus_scalping.rob974_features import (
    FOUR_HOUR_MS,
    MINUTE_MS,
    Bar4h,
    MinuteBar,
)

from .constants import HARD_STOP_WATCH_HOURS, LOCKED_LIMITS
from .strategy import TrendBar4h, required_close_times

_PAGE_MINUTES = 500
_BUCKET_MINUTES = 240


@dataclass(frozen=True)
class TrendMinuteBar(MinuteBar):
    low_text: str
    high_text: str


def minute_low(bar: MinuteBar) -> Decimal:
    return (
        Decimal(bar.low_text)
        if isinstance(bar, TrendMinuteBar)
        else Decimal(str(bar.low))
    )


def minute_high(bar: MinuteBar) -> Decimal:
    return (
        Decimal(bar.high_text)
        if isinstance(bar, TrendMinuteBar)
        else Decimal(str(bar.high))
    )


async def _fetch_window(
    transport: object,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
    check: Callable[[], None],
    raw_prices: dict[int, tuple[str, str, str, str]],
) -> tuple[MinuteBar, ...]:
    """Rows in ``[start_ms, end_ms)``; ``check`` re-validates the origin per page."""
    if symbol not in LOCKED_LIMITS.universe or start_ms >= end_ms:
        raise ValueError("invalid trend minute window")

    def keep(ts: int, prices: tuple[str, str, str, str]) -> None:
        if any(not isinstance(value, str) for value in prices):
            raise ValueError("trend requires exact decimal OHLC text")
        if any(not Decimal(v).is_finite() or Decimal(v) <= 0 for v in prices):
            raise ValueError("invalid decimal OHLC")
        raw_prices[ts] = prices

    by_ts: dict[int, MinuteBar] = {}
    for cursor in range(start_ms, end_ms, _PAGE_MINUTES * MINUTE_MS):
        stop = min(cursor + _PAGE_MINUTES * MINUTE_MS, end_ms)
        check()
        rows = await rob993_bars.fetch_1m_minute_bars(
            transport,  # type: ignore[arg-type]
            symbol,
            limit=_PAGE_MINUTES,
            start_time_ms=cursor,
            end_time_ms=stop - 1,
            raw_price_sink=keep,
        )
        for row in rows:
            if cursor <= row.ts < stop:
                by_ts[row.ts] = row
    return tuple(by_ts[ts] for ts in sorted(by_ts))


async def collect_signal_bars(
    transport: object,
    symbol: str,
    *,
    decision_ts: int,
    lookback_days: int,
    check: Callable[[], None],
) -> tuple[Bar4h, ...]:
    """The complete 4h bars the signal and volatility need (absent = absent)."""
    if decision_ts % FOUR_HOUR_MS:
        raise ValueError("trend decision must be 4h aligned")
    raw: dict[int, tuple[str, str, str, str]] = {}
    minutes: list[MinuteBar] = []
    for close_ts in required_close_times(decision_ts, lookback_days):
        minutes.extend(
            await _fetch_window(
                transport,
                symbol,
                start_ms=close_ts - _BUCKET_MINUTES * MINUTE_MS,
                end_ms=close_ts,
                check=check,
                raw_prices=raw,
            )
        )
    complete = rob993_bars.build_complete_4h(tuple(minutes))
    exact: list[Bar4h] = []
    for bar in complete:
        last = raw[bar.close_ts - MINUTE_MS]
        exact.append(TrendBar4h(**asdict(bar), close_text=last[3]))
    return tuple(exact)


async def collect_hard_stop_minutes(
    transport: object,
    symbol: str,
    *,
    entered_ms: int,
    now_ms: int,
    check: Callable[[], None],
) -> tuple[MinuteBar, ...]:
    """Complete 1m rows wholly after the fill minute, at most the last 24 hours.

    Raises on any transport or contract failure: the caller turns that into the
    "4h decision only" fallback and never into a partial 1m picture.
    """
    window_start = max(entered_ms, now_ms - HARD_STOP_WATCH_HOURS * 60 * MINUTE_MS)
    # The fill minute can hold extrema from before the fill: start at the next.
    start = (window_start // MINUTE_MS + 1) * MINUTE_MS
    end = (now_ms // MINUTE_MS) * MINUTE_MS
    if start >= end:
        return ()
    raw: dict[int, tuple[str, str, str, str]] = {}
    rows = await _fetch_window(
        transport, symbol, start_ms=start, end_ms=end, check=check, raw_prices=raw
    )
    return tuple(
        TrendMinuteBar(**asdict(row), low_text=raw[row.ts][2], high_text=raw[row.ts][1])
        for row in rows
    )
