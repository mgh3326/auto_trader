from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.routers import trading
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot


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
    assert result.ask[0].price == 70100.0
    assert result.ask[0].quantity == 123
    assert len(result.bid) == 1
    assert result.bid[0].price == 70000.0
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
