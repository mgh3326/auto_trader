"""ROB-285 — Binance public REST client (read-only, no API key required)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.brokers.binance.rest_client import BinancePublicRestClient


@pytest.mark.asyncio
async def test_exchange_info_returns_symbol_metadata(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [],
                }
            ],
        },
    )
    async with BinancePublicRestClient() as rest:
        info = await rest.exchange_info("BTCUSDT")
    assert info.symbol == "BTCUSDT"
    assert info.base_asset == "BTC"
    assert info.quote_asset == "USDT"
    assert info.status == "TRADING"


@pytest.mark.asyncio
async def test_klines_returns_list_of_dtos(httpx_mock) -> None:
    # Binance kline row shape:
    #   [openTime, open, high, low, close, vol, closeTime, quoteVol,
    #    trades, takerBuyBase, takerBuyQuote, ignore]
    httpx_mock.add_response(
        url=(
            "https://api.binance.com/api/v3/klines?"
            "symbol=BTCUSDT&interval=1m&limit=1000"
        ),
        json=[
            [
                1700000000000,
                "30000.0",
                "30100.0",
                "29900.0",
                "30050.0",
                "12.5",
                1700000059999,
                "375625.0",
                100,
                "6.0",
                "180300.0",
                "0",
            ]
        ],
    )
    async with BinancePublicRestClient() as rest:
        rows = await rest.klines("BTCUSDT", "1m", limit=1000)
    assert len(rows) == 1
    row = rows[0]
    assert row.open_time == dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=dt.UTC)
    assert float(row.open) == 30000.0
    assert row.is_closed is True  # Past kline; close_time is in the past.


@pytest.mark.asyncio
async def test_book_ticker_returns_bid_ask(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT",
        json={
            "symbol": "BTCUSDT",
            "bidPrice": "30000.0",
            "bidQty": "1.0",
            "askPrice": "30001.0",
            "askQty": "1.5",
        },
    )
    async with BinancePublicRestClient() as rest:
        bt = await rest.book_ticker("BTCUSDT")
    assert float(bt.bid_price) == 30000.0
    assert float(bt.ask_price) == 30001.0


@pytest.mark.asyncio
async def test_rest_client_does_not_expose_signed_methods() -> None:
    rest = BinancePublicRestClient()
    for forbidden in (
        "account",
        "order",
        "open_orders",
        "my_trades",
        "cancel_order",
        "user_data_stream",
    ):
        assert not hasattr(rest, forbidden), (
            f"Public adapter exposes {forbidden} — scope breach. "
            "Signed-endpoint surface belongs in Child C testnet adapter, "
            "not the public adapter."
        )
    await rest.aclose()
