"""ROB-307 PR1 — tests for the read-only Demo-host market-data adapter.

The adapter reads klines + bookTicker from Demo hosts only
(``demo-api.binance.com`` for spot, ``demo-fapi.binance.com`` for
futures) over unsigned GETs. It must fail closed on any non-Demo host so
the signal path can never reach ``api.binance.com``. Network is mocked
with ``httpx_mock``; no credentials, no signing, no order endpoints.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.market_data import (
    DEMO_DATA_HOSTS,
    BookTicker,
    DemoDataHostBlocked,
    DemoScalpingMarketData,
    assert_demo_data_host,
    data_age_seconds,
    spread_bps,
)
from app.services.brokers.binance.demo_scalping.signal import Candle

_SPOT_KLINES = [
    [
        1779608160000,
        "1.35960000",
        "1.35980000",
        "1.35950000",
        "1.35955000",
        "6136.00000000",
        1779608219999,
        "8342.58965000",
        46,
        "2795.80000000",
        "3801.21357000",
        "0",
    ],
    [
        1779608220000,
        "1.35955000",
        "1.35990000",
        "1.35940000",
        "1.35970000",
        "685.40000000",
        1779608279999,
        "931.84008000",
        12,
        "59.70000000",
        "81.16812000",
        "0",
    ],
]


def test_demo_data_hosts_are_demo_only() -> None:
    assert DEMO_DATA_HOSTS == frozenset(
        {"demo-api.binance.com", "demo-fapi.binance.com"}
    )


def test_assert_demo_data_host_rejects_live_host() -> None:
    assert_demo_data_host("demo-api.binance.com")  # no raise
    assert_demo_data_host("demo-fapi.binance.com")  # no raise
    with pytest.raises(DemoDataHostBlocked):
        assert_demo_data_host("api.binance.com")
    with pytest.raises(DemoDataHostBlocked):
        assert_demo_data_host("demo-api.binance.com.evil.example")


def test_spread_bps_from_book_ticker() -> None:
    book = BookTicker(bid=Decimal("100"), ask=Decimal("100.10"))
    # mid = 100.05, spread = 0.10 -> ~9.995 bps
    assert spread_bps(book) == pytest.approx(Decimal("9.995"), rel=Decimal("0.001"))


def test_data_age_seconds_measures_from_open_time() -> None:
    # Binance returns the in-progress candle (close_time in the future), so
    # freshness is measured from the candle's open_time.
    candle = Candle(
        open_time_ms=1_000_000,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        close_time_ms=1_059_999,
    )
    assert data_age_seconds(candle, now_ms=1_030_000) == 30.0


def test_data_age_seconds_clamps_future_to_zero() -> None:
    # Clock skew must never yield a negative age.
    candle = Candle(
        open_time_ms=2_000_000,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        close_time_ms=2_059_999,
    )
    assert data_age_seconds(candle, now_ms=1_999_000) == 0.0


@pytest.mark.asyncio
async def test_fetch_klines_spot_parses_to_candles(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/klines\?.*$"),
        json=_SPOT_KLINES,
    )
    md = DemoScalpingMarketData()
    candles = await md.fetch_klines("spot", "XRPUSDT", interval="1m", limit=2)
    assert len(candles) == 2
    assert candles[0] == Candle(
        open_time_ms=1779608160000,
        open=Decimal("1.35960000"),
        high=Decimal("1.35980000"),
        low=Decimal("1.35950000"),
        close=Decimal("1.35955000"),
        close_time_ms=1779608219999,
    )
    assert candles[-1].close == Decimal("1.35970000")


@pytest.mark.asyncio
async def test_fetch_klines_futures_uses_demo_fapi_host(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/klines\?.*$"),
        json=_SPOT_KLINES,
    )
    md = DemoScalpingMarketData()
    candles = await md.fetch_klines("usdm_futures", "XRPUSDT", interval="1m", limit=2)
    assert len(candles) == 2
    request = httpx_mock.get_requests()[0]
    assert request.url.host == "demo-fapi.binance.com"
    assert "/fapi/v1/klines" in str(request.url)


@pytest.mark.asyncio
async def test_fetch_book_ticker_spot_parses_bid_ask(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"^https://demo-api\.binance\.com/api/v3/ticker/bookTicker\?.*$"
        ),
        json={
            "symbol": "XRPUSDT",
            "bidPrice": "1.35950000",
            "bidQty": "7033.50000000",
            "askPrice": "1.35960000",
            "askQty": "28609.70000000",
        },
    )
    md = DemoScalpingMarketData()
    book = await md.fetch_book_ticker("spot", "XRPUSDT")
    assert book == BookTicker(bid=Decimal("1.35950000"), ask=Decimal("1.35960000"))
