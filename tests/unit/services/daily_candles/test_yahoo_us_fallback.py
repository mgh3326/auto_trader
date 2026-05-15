from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.daily_candles.yahoo_us_fallback import (
    fetch_us_daily_yahoo_fallback,
)


class TestFetchUsDailyYahooFallback:
    @pytest.mark.asyncio
    async def test_returns_canonical_rows_with_adj_close(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=3, freq="B"),
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "adj_close": [99.0, 100.0, 101.0],
                "volume": [1000, 1100, 1200],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="ILLIQUIDETF", n=3)
        assert len(rows) == 3
        assert rows[0].adj_close == 99.0
        assert all(r.symbol == "ILLIQUIDETF" for r in rows)

    @pytest.mark.asyncio
    async def test_handles_missing_adj_close_column(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=2, freq="B"),
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1000, 1100],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="X", n=2)
        assert all(r.adj_close is None for r in rows)
