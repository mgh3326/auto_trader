"""Bounded paged use of the ROB-993 complete-only 1m to 4h collector."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

from app.services.brokers.binance.demo_strategy_loop import bars as rob993_bars
from research.nautilus_scalping.rob974_features import (
    FOUR_HOUR_MS,
    MINUTE_MS,
    Bar4h,
    MinuteBar,
)

from .client import H5DemoClient, assert_h5_client
from .strategy import UNIVERSE, H5Bar4h

_PAGE_MINUTES = 500
_MAX_PAGES = 20


@dataclass(frozen=True)
class H5MinuteBar(MinuteBar):
    price_text: tuple[str, str, str, str]


def minute_price(bar: MinuteBar, field: str) -> Decimal:
    if isinstance(bar, H5MinuteBar):
        return Decimal(bar.price_text[("open", "high", "low", "close").index(field)])
    return Decimal(str(getattr(bar, field)))


def _complete_exact(
    minutes: tuple[MinuteBar, ...], raw: dict[int, tuple[str, str, str, str]]
) -> tuple[Bar4h, ...]:
    complete = rob993_bars.build_complete_4h(minutes)
    exact: list[Bar4h] = []
    for bar in complete:
        bucket = [raw[row.ts] for row in minutes if bar.ts <= row.ts < bar.close_ts]
        exact.append(
            H5Bar4h(
                **asdict(bar),
                price_text=(
                    bucket[0][0],
                    max((row[1] for row in bucket), key=Decimal),
                    min((row[2] for row in bucket), key=Decimal),
                    bucket[-1][3],
                ),
            )
        )
    return tuple(exact)


async def collect_minutes(
    client: H5DemoClient,
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
    raw_prices: dict[int, tuple[str, str, str, str]] | None = None,
) -> tuple[MinuteBar, ...]:
    """Fetch a bounded exact interval; absent 1m rows remain absent."""
    assert_h5_client(client)
    if symbol not in UNIVERSE or start_ms >= end_ms:
        raise ValueError("invalid H5 minute window")
    pages = (end_ms - start_ms + _PAGE_MINUTES * MINUTE_MS - 1) // (
        _PAGE_MINUTES * MINUTE_MS
    )
    if pages > _MAX_PAGES:
        raise ValueError("H5 bar window exceeds bounded page budget")
    by_ts: dict[int, MinuteBar] = {}

    def keep_price(ts: int, prices: tuple[str, str, str, str]) -> None:
        if any(not isinstance(value, str) for value in prices):
            raise ValueError("H5 requires exact decimal OHLC text")
        if any(
            not Decimal(value).is_finite() or Decimal(value) <= 0 for value in prices
        ):
            raise ValueError("invalid H5 decimal OHLC")
        if raw_prices is not None:
            raw_prices[ts] = prices

    for cursor in range(start_ms, end_ms, _PAGE_MINUTES * MINUTE_MS):
        stop = min(cursor + _PAGE_MINUTES * MINUTE_MS, end_ms)
        assert_h5_client(client)
        rows = await rob993_bars.fetch_1m_minute_bars(
            client._client,
            symbol,
            limit=_PAGE_MINUTES,
            start_time_ms=cursor,
            end_time_ms=stop - 1,
            raw_price_sink=keep_price if raw_prices is not None else None,
        )
        for row in rows:
            if cursor <= row.ts < stop:
                by_ts[row.ts] = row
    return tuple(by_ts[ts] for ts in sorted(by_ts))


async def collect_signal_history(
    client: H5DemoClient, symbol: str, *, decision_ts: int
) -> tuple[Bar4h, ...]:
    """21 completed 4h bars require 5,040 minute rows, not ROB-993's 500."""
    if decision_ts % FOUR_HOUR_MS:
        raise ValueError("H5 decision must be 4h aligned")
    raw: dict[int, tuple[str, str, str, str]] = {}
    minutes = await collect_minutes(
        client,
        symbol,
        start_ms=decision_ts - 21 * FOUR_HOUR_MS,
        end_ms=decision_ts,
        raw_prices=raw,
    )
    return _complete_exact(minutes, raw)


async def collect_holding_history(
    client: H5DemoClient,
    symbol: str,
    *,
    entered_ms: int,
    now_ms: int,
) -> tuple[tuple[MinuteBar, ...], tuple[Bar4h, ...]]:
    """Observe adverse 1m extremes between four-hour closes and after restart."""
    if entered_ms >= now_ms:
        return (), ()
    raw: dict[int, tuple[str, str, str, str]] = {}
    minutes = await collect_minutes(
        client,
        symbol,
        # Fetch the whole containing 4h bucket so its first post-entry
        # close is eligible for the complete-bar stop. Adverse extremes
        # below are restricted to minutes since the actual fill.
        start_ms=(entered_ms // FOUR_HOUR_MS) * FOUR_HOUR_MS,
        # A late restart must still reach its time exit within the page
        # budget. The first 24h is the declared envelope; the caller also
        # checks the current executable quote and elapsed wall clock.
        end_ms=(min(now_ms, entered_ms + 6 * FOUR_HOUR_MS) // MINUTE_MS) * MINUTE_MS,
        raw_prices=raw,
    )
    complete = _complete_exact(minutes, raw)
    # The containing minute may have extrema from before the fill. Only
    # complete minutes wholly after it can prove a historical hard-stop touch.
    since_fill = tuple(
        H5MinuteBar(**asdict(row), price_text=raw[row.ts])
        for row in minutes
        if row.ts >= entered_ms
    )
    return since_fill, complete


def complete_closes(bars: Sequence[Bar4h]) -> tuple[int, ...]:
    return tuple(bar.close_ts for bar in bars)
