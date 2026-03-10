import asyncio
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.models.manual_holdings import MarketType
from app.routers import trading
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_get_orderbook_uses_new_quantity_keys_first(monkeypatch):
    get_orderbook_mock = AsyncMock(
        return_value=OrderbookSnapshot(
            symbol="005930",
            instrument_type="equity_kr",
            source="kis",
            asks=[OrderbookLevel(price=70100, quantity=123)],
            bids=[OrderbookLevel(price=70000, quantity=321)],
            total_ask_qty=123,
            total_bid_qty=321,
            bid_ask_ratio=2.61,
            expected_price=70050,
            expected_qty=10,
        )
    )
    monkeypatch.setattr(
        trading.market_data_service, "get_orderbook", get_orderbook_mock
    )

    result = await trading.get_orderbook(
        ticker="005930",
        market_type=MarketType.KR,
        current_user=MagicMock(),
        db=AsyncMock(),
    )

    assert len(result.ask) == 1
    assert result.ask[0].price == pytest.approx(70100.0)
    assert result.ask[0].quantity == 123
    assert len(result.bid) == 1
    assert result.bid[0].price == pytest.approx(70000.0)
    assert result.bid[0].quantity == 321
    get_orderbook_mock.assert_awaited_once_with("005930", "kr")


@pytest.mark.asyncio
async def test_get_orderbook_falls_back_to_legacy_quantity_keys(monkeypatch):
    monkeypatch.setattr(
        trading.market_data_service,
        "get_orderbook",
        AsyncMock(
            return_value=OrderbookSnapshot(
                symbol="005930",
                instrument_type="equity_kr",
                source="kis",
                asks=[OrderbookLevel(price=70200, quantity=44)],
                bids=[OrderbookLevel(price=69900, quantity=55)],
                total_ask_qty=44,
                total_bid_qty=55,
                bid_ask_ratio=1.25,
                expected_price=None,
                expected_qty=None,
            )
        ),
    )

    result = await trading.get_orderbook(
        ticker="005930",
        market_type=MarketType.KR,
        current_user=MagicMock(),
        db=AsyncMock(),
    )

    assert len(result.ask) == 1
    assert result.ask[0].quantity == 44
    assert len(result.bid) == 1
    assert result.bid[0].quantity == 55


@pytest.mark.asyncio
async def test_get_ohlcv_rejects_non_positive_days(monkeypatch):
    class DummyKISClient:
        async def inquire_daily_itemchartprice(
            self, code: str, market: str = "UN", n: int = 200, period: str = "D"
        ):
            await asyncio.sleep(0)
            raise AssertionError("KIS client should not be called for invalid days")

    monkeypatch.setattr(trading, "KISClient", DummyKISClient)

    with pytest.raises(HTTPException, match="days") as exc_info:
        await trading.get_ohlcv(
            ticker="005930",
            days=0,
            market_type=MarketType.KR,
            current_user=MagicMock(),
            db=AsyncMock(),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_ohlcv_clamps_requested_days_before_kis_call(monkeypatch):
    captured: dict[str, int] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(
            self, code: str, market: str = "UN", n: int = 200, period: str = "D"
        ):
            captured["n"] = n
            return pd.DataFrame(
                [
                    {
                        "date": pd.Timestamp("2026-03-10"),
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "volume": 1,
                    }
                ]
            )

    monkeypatch.setattr(trading, "KISClient", DummyKISClient)

    result = await trading.get_ohlcv(
        ticker="005930",
        days=9999,
        market_type=MarketType.KR,
        current_user=MagicMock(),
        db=AsyncMock(),
    )

    assert captured["n"] == 200
    assert result.ticker == "005930"
    assert len(result.data) == 1


def test_get_ohlcv_route_documents_400_response() -> None:
    route = next(
        route
        for route in trading.router.routes
        if isinstance(route, APIRoute) and route.path == "/trading/api/v1/trading/ohlcv"
    )

    assert 400 in route.responses


@pytest.mark.asyncio
async def test_get_current_price_uses_kis_inquire_price_for_kr():
    class DummyKISClient:
        async def inquire_price(self, code: str, market: str = "UN") -> pd.DataFrame:
            return await asyncio.sleep(0, result=pd.DataFrame([{"close": 70123.0}]))

        async def inquire_overseas_daily_price(
            self,
            symbol: str,
            exchange_code: str = "NASD",
            n: int = 200,
            period: str = "D",
        ) -> pd.DataFrame:
            await asyncio.sleep(0)
            raise AssertionError("US price path should not be used for KR")

    price = await trading._get_current_price(
        DummyKISClient(),
        ticker="005930",
        market_type=MarketType.KR,
        db=AsyncMock(),
    )

    assert price == pytest.approx(70123.0)


@pytest.mark.asyncio
async def test_get_current_price_fails_closed_for_us_without_live_quote(monkeypatch):
    called = False

    class DummyKISClient:
        async def inquire_price(self, code: str, market: str = "UN") -> pd.DataFrame:
            await asyncio.sleep(0)
            raise AssertionError("KR price path should not be used for US")

        async def inquire_overseas_daily_price(
            self,
            symbol: str,
            exchange_code: str = "NASD",
            n: int = 200,
            period: str = "D",
        ) -> pd.DataFrame:
            nonlocal called
            called = True
            return await asyncio.sleep(0, result=pd.DataFrame([{"close": 211.5}]))

    monkeypatch.setattr(
        trading, "_resolve_exchange_code", AsyncMock(return_value="NYS")
    )

    price = await trading._get_current_price(
        DummyKISClient(),
        ticker="AAPL",
        market_type=MarketType.US,
        db=AsyncMock(),
    )

    assert price == 0
    assert called is False
