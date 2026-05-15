from datetime import date
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.daily_candles.kis_daily_fetcher import (
    fetch_kr_daily_unclamped,
    fetch_us_daily_unclamped,
)


class TestFetchKrDailyUnclamped:
    @pytest.mark.asyncio
    async def test_requests_full_horizon_bypassing_display_clamp(self):
        # Sample frame larger than the display clamp value.
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=300, freq="B"),
                "open": [100.0] * 300,
                "high": [101.0] * 300,
                "low": [99.0] * 300,
                "close": [100.5] * 300,
                "volume": [1000] * 300,
                "value": [100500] * 300,
            }
        )
        kis = type("StubKIS", (), {})()
        kis.inquire_daily_itemchartprice_unclamped = AsyncMock(return_value=frame)

        out = await fetch_kr_daily_unclamped(
            kis=kis, code="005930", n=300, end_date=date(2025, 1, 1)
        )

        kis.inquire_daily_itemchartprice_unclamped.assert_awaited_once_with(
            code="005930", market="J", n=300, period="D", end_date=date(2025, 1, 1)
        )
        assert len(out) == 300


class TestFetchUsDailyUnclamped:
    @pytest.mark.asyncio
    async def test_passes_iteration_cap_for_long_horizon(self):
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=400, freq="B"),
                "open": [100.0] * 400,
                "high": [101.0] * 400,
                "low": [99.0] * 400,
                "close": [100.5] * 400,
                "volume": [1000] * 400,
            }
        )
        kis = type("StubKIS", (), {})()
        kis.inquire_overseas_daily_price_unclamped = AsyncMock(return_value=frame)

        out = await fetch_us_daily_unclamped(
            kis=kis, symbol="AAPL", exchange_code="NASD", n=400
        )

        kis.inquire_overseas_daily_price_unclamped.assert_awaited_once_with(
            symbol="AAPL",
            exchange_code="NASD",
            n=400,
            period="D",
        )
        assert len(out) == 400
