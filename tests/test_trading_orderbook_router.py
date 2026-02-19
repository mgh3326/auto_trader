from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.routers import trading


@pytest.mark.asyncio
async def test_get_orderbook_uses_new_quantity_keys_first(monkeypatch):
    class DummyKISClient:
        async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
            return {
                "askp1": "70100",
                "askp_rsqn1": "123",
                "askp1_rsqn": "999",
                "bidp1": "70000",
                "bidp_rsqn1": "321",
                "bidp1_rsqn": "888",
            }

    monkeypatch.setattr(trading, "KISClient", DummyKISClient)

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


@pytest.mark.asyncio
async def test_get_orderbook_falls_back_to_legacy_quantity_keys(monkeypatch):
    class DummyKISClient:
        async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
            return {
                "askp1": "70200",
                "askp1_rsqn": "44",
                "bidp1": "69900",
                "bidp1_rsqn": "55",
            }

    monkeypatch.setattr(trading, "KISClient", DummyKISClient)

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
