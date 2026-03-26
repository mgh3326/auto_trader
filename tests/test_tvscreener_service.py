from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.tvscreener_service import (
    TvScreenerService,
)


class FakeQuery:
    """Mock query where sort_by returns None"""

    def __init__(self, data, return_none_on_sort=False):
        self.data = data
        self.return_none_on_sort = return_none_on_sort
        self.sort_applied = False
        self.sort_field = None
        self.sort_ascending = None

    def where(self, condition):
        return self

    def sort_by(self, field, ascending=True):
        if self.return_none_on_sort:
            return None
        self.sort_applied = True
        self.sort_field = field
        self.sort_ascending = ascending
        return self

    def set_range(self, start, end):
        return self

    def get(self):
        return self.data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_crypto_screener_handles_sort_by_none():
    """Test fallback behavior when sort_by() returns None"""

    # Given: Mock object where sort_by returns None
    fake_df = pd.DataFrame(
        {
            "name": ["BTC", "ETH", "XRP"],
            "price": [100_000_000, 5_000_000, 1_000],
            "value_traded": [900e9, 1200e9, 700e9],
        }
    )

    fake_screener = MagicMock()
    fake_query = FakeQuery(fake_df, return_none_on_sort=True)
    fake_screener.select.return_value = fake_query

    # Create mock enums with .name property
    class MockField:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return f"CryptoField.{self.name}"

    fake_tvscreener = SimpleNamespace(
        CryptoScreener=lambda: fake_screener,
        CryptoField=SimpleNamespace(
            NAME=MockField("NAME"),
            PRICE=MockField("PRICE"),
            VALUE_TRADED=MockField("VALUE_TRADED"),
        ),
    )

    service = TvScreenerService()

    # When & Then: Should return result without exception even if sort_by fails
    with patch(
        "app.services.tvscreener_service._import_tvscreener",
        return_value=fake_tvscreener,
    ):
        # Currently this test should fail with TvScreenerError
        result = await service.query_crypto_screener(
            columns=[
                fake_tvscreener.CryptoField.NAME,
                fake_tvscreener.CryptoField.PRICE,
            ],
            sort_by=fake_tvscreener.CryptoField.VALUE_TRADED,
            ascending=False,
            limit=10,
        )

        # Fallback: Check if sorted at Python level
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        # If sorted by value_traded descending: ETH (1200e9), BTC (900e9), XRP (700e9)
        assert result.iloc[0]["name"] == "ETH"
        assert result.iloc[1]["name"] == "BTC"
        assert result.iloc[2]["name"] == "XRP"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_stock_screener_handles_sort_by_none():
    """Test fallback behavior when StockScreener.sort_by() returns None"""

    # Given: Mock object where sort_by returns None
    fake_df = pd.DataFrame(
        {
            "name": ["AAPL", "MSFT", "GOOGL"],
            "price": [150, 300, 2800],
            "market_capitalization": [2.5e12, 2.2e12, 1.8e12],
        }
    )

    fake_screener = MagicMock()
    fake_query = FakeQuery(fake_df, return_none_on_sort=True)
    fake_screener.select.return_value = fake_query

    # Create mock enums with .name property
    class MockField:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return f"StockField.{self.name}"

    fake_tvscreener = SimpleNamespace(
        StockScreener=lambda: fake_screener,
        StockField=SimpleNamespace(
            NAME=MockField("NAME"),
            PRICE=MockField("PRICE"),
            MARKET_CAP=MockField("MARKET_CAP"),
            COUNTRY=MockField("COUNTRY"),
        ),
    )

    service = TvScreenerService()

    # When & Then: Should return result without exception even if sort_by fails
    with patch(
        "app.services.tvscreener_service._import_tvscreener",
        return_value=fake_tvscreener,
    ):
        result = await service.query_stock_screener(
            columns=[fake_tvscreener.StockField.NAME, fake_tvscreener.StockField.PRICE],
            sort_by=fake_tvscreener.StockField.MARKET_CAP,
            ascending=False,
            limit=10,
        )

        # Fallback: Check if sorted at Python level
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        # If sorted by market_cap descending: AAPL (2.5e12), MSFT (2.2e12), GOOGL (1.8e12)
        assert result.iloc[0]["name"] == "AAPL"
        assert result.iloc[1]["name"] == "MSFT"
        assert result.iloc[2]["name"] == "GOOGL"
