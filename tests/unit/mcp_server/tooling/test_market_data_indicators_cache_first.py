from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services.daily_candles.repository import MarketKey


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
    async def test_crypto_db_hit_uses_canonical_upbit_partition(self):
        from app.mcp_server.tooling.market_data_indicators import (
            _fetch_ohlcv_for_indicators,
        )

        rows = [
            _make_row(
                "KRW-BTC",
                "upbit_krw",
                datetime.now(UTC) - timedelta(days=i),
                100_000_000.0 + i,
                source="upbit",
            )
            for i in range(10)
        ]
        session = MagicMock()

        class SessionFactory:
            async def __aenter__(self) -> object:
                return session

            async def __aexit__(self, *args: object) -> None:
                return None

        fetch_recent = AsyncMock(return_value=list(reversed(rows)))
        with (
            patch("app.core.db.AsyncSessionLocal", return_value=SessionFactory()),
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
                new=fetch_recent,
            ),
            patch(
                "app.mcp_server.tooling.market_data_indicators._cache_is_fresh_crypto",
                return_value=True,
            ),
            patch(
                "app.mcp_server.tooling.market_data_indicators.upbit_service.fetch_ohlcv",
                new=AsyncMock(),
            ) as upbit_fetch,
        ):
            df = await _fetch_ohlcv_for_indicators("KRW-BTC", "crypto", count=10)

        assert len(df) == 10
        fetch_recent.assert_awaited_once_with(
            market=MarketKey.CRYPTO,
            symbol="KRW-BTC",
            partition="upbit_krw",
            count=10,
        )
        upbit_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_crypto_live_miss_writes_canonical_upbit_partition(self):
        from app.mcp_server.tooling.market_data_indicators import (
            _fetch_ohlcv_for_indicators,
        )

        frame = pd.DataFrame(
            {
                "date": pd.date_range("2026-06-01", periods=2, freq="D"),
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "volume": [10.0, 11.0],
                "value": [1010.0, 1122.0],
            }
        )
        session = MagicMock()
        session.commit = AsyncMock()

        class SessionFactory:
            async def __aenter__(self) -> object:
                return session

            async def __aexit__(self, *args: object) -> None:
                return None

        upsert = AsyncMock(return_value=2)
        with (
            patch("app.core.db.AsyncSessionLocal", return_value=SessionFactory()),
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.fetch_recent",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.daily_candles.repository.DailyCandlesRepository.upsert_rows",
                new=upsert,
            ),
            patch(
                "app.mcp_server.tooling.market_data_indicators.upbit_service.fetch_ohlcv",
                new=AsyncMock(return_value=frame),
            ),
        ):
            df = await _fetch_ohlcv_for_indicators("KRW-BTC", "crypto", count=2)

        assert len(df) == 2
        upsert.assert_awaited_once()
        assert upsert.await_args.kwargs["market"] is MarketKey.CRYPTO
        assert {row.partition for row in upsert.await_args.kwargs["rows"]} == {
            "upbit_krw"
        }
        session.commit.assert_awaited_once()

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
