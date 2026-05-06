"""Unit tests for InvestQuoteService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.invest_quote_service import InvestQuoteService


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_kr_prices() -> None:
    # Mock client and market data
    kis_client = MagicMock()
    db = MagicMock()

    service = InvestQuoteService(kis_client, db)

    # Mock MarketDataClient.inquire_price
    service._market_data = AsyncMock()

    # Mock return value: DataFrame indexed by code
    df = pd.DataFrame([{"close": 70000.0}], index=["005930"])
    service._market_data.inquire_price.return_value = df

    prices = await service.fetch_kr_prices(["005930"])

    assert prices == pytest.approx({"005930": 70000.0})
    service._market_data.inquire_price.assert_called_once_with("005930", market="J")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_us_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    kis_client = MagicMock()
    db = MagicMock()

    service = InvestQuoteService(kis_client, db)

    # Mock get_us_exchange_by_symbol
    mock_get_exchange = AsyncMock(return_value="NASD")
    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol", mock_get_exchange
    )

    # Mock MarketDataClient.inquire_overseas_daily_price
    service._market_data = AsyncMock()
    df = pd.DataFrame([{"close": 150.0}], index=[0])
    service._market_data.inquire_overseas_daily_price.return_value = df

    prices = await service.fetch_us_prices(["AAPL"])

    assert prices == pytest.approx({"AAPL": 150.0})
    mock_get_exchange.assert_called_once_with("AAPL", db)
    service._market_data.inquire_overseas_daily_price.assert_called_once_with(
        "AAPL", exchange_code="NASD", n=1, period="D"
    )
