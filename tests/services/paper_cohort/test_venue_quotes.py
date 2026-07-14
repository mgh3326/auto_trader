from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.binance.dto import BinanceBookTicker
from app.services.paper_cohort.venue_quotes import (
    AlpacaCryptoQuoteClient,
    ProductionVenueQuoteProvider,
)

pytestmark = pytest.mark.unit


class FakeBinance:
    async def book_ticker(self, symbol: str) -> BinanceBookTicker:
        return BinanceBookTicker(
            symbol=symbol,
            bid_price=Decimal("100"),
            bid_qty=Decimal("2"),
            ask_price=Decimal("101"),
            ask_qty=Decimal("3"),
            fetched_at=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_production_quotes_use_distinct_exact_venue_read_boundaries() -> None:
    seen: list[tuple[str, str]] = []

    def data_handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "quotes": {
                    "BTC/USD": {
                        "bp": "200",
                        "bs": "4",
                        "ap": "202",
                        "as": "5",
                        "t": "2026-07-14T01:00:01Z",
                    }
                }
            },
        )

    def asset_handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(
            200,
            json={
                "min_order_size": "0.00001",
                "min_trade_increment": "0.000001",
            },
        )

    async with (
        httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            transport=httpx.MockTransport(data_handler),
        ) as data_client,
        httpx.AsyncClient(
            base_url=PAPER_TRADING_BASE_URL,
            transport=httpx.MockTransport(asset_handler),
        ) as asset_client,
    ):
        alpaca = AlpacaCryptoQuoteClient(
            data_client=data_client,
            asset_client=asset_client,
            alpaca_settings=AlpacaPaperSettings(
                api_key="test-key", api_secret="test-secret"
            ),
        )
        provider = ProductionVenueQuoteProvider(FakeBinance(), alpaca)  # type: ignore[arg-type]
        binance = await provider.get_quote("binance", "BTCUSDT")
        alpaca_quote = await provider.get_quote("alpaca", "BTCUSDT")

    assert binance.ask_price == Decimal("101")
    assert binance.qty_increment is None
    assert alpaca_quote.symbol == "BTC/USD"
    assert alpaca_quote.ask_price == Decimal("202")
    assert alpaca_quote.qty_increment == Decimal("0.000001")
    assert alpaca_quote.min_qty == Decimal("0.00001")
    assert seen == [
        (
            "GET",
            "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols=BTC%2FUSD",
        ),
        ("GET", f"{PAPER_TRADING_BASE_URL}/v2/assets/BTCUSD"),
    ]
