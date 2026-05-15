from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    DailyCandleSyncService,
    SyncTarget,
)


class TestSyncOneSymbol:
    @pytest.mark.asyncio
    async def test_kis_kr_path_upserts_with_source_kis(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=10)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()

        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="B"),
                "open": [100.0] * 10,
                "high": [101.0] * 10,
                "low": [99.0] * 10,
                "close": [100.5] * 10,
                "volume": [1000] * 10,
                "value": [100500] * 10,
            }
        )
        kis_fetcher = AsyncMock(return_value=frame)
        yahoo_fetcher = AsyncMock(return_value=[])

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=kis_fetcher,
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        result = await svc.sync_one(
            target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
            horizon_bars=400,
        )

        kis_fetcher.assert_awaited_once()
        yahoo_fetcher.assert_not_awaited()
        repo.upsert_rows.assert_awaited_once()
        upserted_rows = repo.upsert_rows.await_args.kwargs["rows"]
        assert all(r.source == "kis" for r in upserted_rows)
        assert result.rows_upserted == 10
        repo.session.commit.assert_awaited_once()
        repo.session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_us_kis_empty_falls_back_to_yahoo(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=5)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock()
        repo.session.rollback = AsyncMock()

        from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

        yahoo_rows = [
            YahooFallbackRow(
                time_utc=datetime(2024, 5, day, tzinfo=UTC),
                symbol="ILLIQUID",
                open=10.0, high=11.0, low=9.0, close=10.5,
                adj_close=10.4, volume=100.0, value=1050.0,
            )
            for day in range(1, 6)
        ]

        kis_us_fetcher = AsyncMock(return_value=pd.DataFrame())
        yahoo_fetcher = AsyncMock(return_value=yahoo_rows)

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=kis_us_fetcher,
            yahoo_us_fetcher=yahoo_fetcher,
            upbit_crypto_fetcher=AsyncMock(),
        )

        result = await svc.sync_one(
            target=SyncTarget(market=MarketKey.US, symbol="ILLIQUID", partition="NASD"),
            horizon_bars=400,
        )

        kis_us_fetcher.assert_awaited_once()
        yahoo_fetcher.assert_awaited_once()
        assert repo.upsert_rows.await_count == 1  # only the yahoo path actually upserts
        upserted_rows = repo.upsert_rows.await_args.kwargs["rows"]
        assert all(r.source == "yahoo_fallback" for r in upserted_rows)
        assert result.rows_upserted == 5
        assert result.fallback_used is True
        repo.session.commit.assert_awaited_once()
        repo.session.rollback.assert_not_awaited()
