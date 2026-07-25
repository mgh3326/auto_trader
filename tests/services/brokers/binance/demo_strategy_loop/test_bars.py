"""ROB-993 — 1m fetch + H1 4h aggregation reuse."""

from __future__ import annotations

import re
import time

import pytest

from app.services.brokers.binance.demo_strategy_loop.bars import (
    build_bars_client,
    fetch_1m_minute_bars,
)
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from research.nautilus_scalping.rob974_features import (
    MINUTE_MS,
    MinuteBar,
    build_complete_4h,
)

_DEMO_HOST = "https://demo-fapi.binance.com"


def _kline_row(open_time_ms: int, close_time_ms: int, price: float) -> list:
    return [
        open_time_ms,
        f"{price}",
        f"{price + 1}",
        f"{price - 1}",
        f"{price + 0.5}",
        "10.0",
        close_time_ms,
        "0",
        1,
        "0",
        "0",
        "0",
    ]


def test_build_bars_client_rejects_non_demo_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        build_bars_client(base_url="https://fapi.binance.com")


@pytest.mark.asyncio
async def test_fetch_1m_minute_bars_drops_in_progress_candle(httpx_mock) -> None:
    now_ms = int(time.time() * 1000)
    minute_aligned_now = now_ms - (now_ms % MINUTE_MS)
    closed_open = minute_aligned_now - 2 * MINUTE_MS
    closed_close = closed_open + MINUTE_MS
    in_progress_open = minute_aligned_now
    in_progress_close = in_progress_open + MINUTE_MS  # still in the future

    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/klines\?.*$"),
        json=[
            _kline_row(closed_open, closed_close, 100.0),
            _kline_row(in_progress_open, in_progress_close, 101.0),
        ],
    )
    client = build_bars_client(base_url=_DEMO_HOST)
    try:
        bars = await fetch_1m_minute_bars(client, "XRPUSDT", limit=2)
    finally:
        await client.aclose()

    assert len(bars) == 1
    assert bars[0].ts == closed_open


@pytest.mark.asyncio
async def test_fetch_1m_minute_bars_empty_response(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/klines\?.*$"),
        json=[],
    )
    client = build_bars_client(base_url=_DEMO_HOST)
    try:
        bars = await fetch_1m_minute_bars(client, "XRPUSDT")
    finally:
        await client.aclose()
    assert bars == ()


def test_build_complete_4h_reused_directly_from_h1() -> None:
    """Sanity: the package re-exports H1's aggregator unchanged (no fork)."""
    start = 0
    rows = tuple(
        MinuteBar(start + i * MINUTE_MS, 1.0, 2.0, 0.5, 1.5, 1.0) for i in range(240)
    )
    bars = build_complete_4h(rows)
    assert len(bars) == 1
    assert bars[0].is_segment_start is True

    from app.services.brokers.binance.demo_strategy_loop.bars import (
        build_complete_4h as reexported,
    )

    assert reexported is build_complete_4h
