from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


def _make_row(
    symbol: str, partition: str, t: datetime, close: float, source: str = "kis"
):
    from app.services.daily_candles.repository import DailyCandleRow

    return DailyCandleRow(
        time_utc=t,
        symbol=symbol,
        partition=partition,
        open=close - 1.0,
        high=close + 0.5,
        low=close - 1.5,
        close=close,
        adj_close=None,
        volume=1000.0,
        value=close * 1000.0,
        source=source,
    )


class TestCacheFirstReadPath:
    @pytest.mark.asyncio
    async def test_kr_db_hit_skips_external_api(self):
        """When DB has fresh, sufficient rows, KIS is not called."""
        from app.mcp_server.tooling.market_data_indicators import (
            _fetch_ohlcv_for_indicators,
        )

        # Fresh = newest row is "today" relative to a deterministic now.
        today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        db_rows = [
            _make_row("005930", "KRX", today - timedelta(days=i), 70000.0 + i)
            for i in range(10)
        ]

        with (
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
                new=AsyncMock(return_value=list(reversed(db_rows))),
            ),
            patch(
                "app.mcp_server.tooling.market_data_indicators._cache_is_fresh_equity",
                return_value=True,
            ),
            patch(
                "app.services.daily_candles.kis_daily_fetcher.fetch_kr_daily_unclamped",
                new=AsyncMock(),
            ) as mock_kis,
        ):
            df = await _fetch_ohlcv_for_indicators("005930", "equity_kr", count=10)

        assert len(df) == 10
        mock_kis.assert_not_called()  # DB hit avoided the external API entirely

    @pytest.mark.asyncio
    async def test_kr_db_miss_falls_back_to_kis_and_upserts(self):
        """When DB is empty, KIS is called and the result is upserted."""
        from app.mcp_server.tooling.market_data_indicators import (
            _fetch_ohlcv_for_indicators,
        )

        kis_frame = pd.DataFrame(
            {
                "date": pd.date_range("2025-12-01", periods=200, freq="B"),
                "open": [70000.0] * 200,
                "high": [71000.0] * 200,
                "low": [69500.0] * 200,
                "close": [70500.0] * 200,
                "volume": [100000] * 200,
                "value": [7050000000] * 200,
            }
        )

        with (
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.daily_candles.kis_daily_fetcher.fetch_kr_daily_unclamped",
                new=AsyncMock(return_value=kis_frame),
            ) as mock_kis,
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
                new=AsyncMock(return_value=200),
            ) as mock_upsert,
        ):
            df = await _fetch_ohlcv_for_indicators("005930", "equity_kr", count=200)

        mock_kis.assert_awaited_once()
        mock_upsert.assert_awaited_once()
        assert len(df) >= 200
