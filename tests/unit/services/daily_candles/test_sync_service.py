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
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                adj_close=10.4,
                volume=100.0,
                value=1050.0,
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

    @pytest.mark.asyncio
    async def test_kr_commit_failure_triggers_rollback(self):
        repo = MagicMock()
        repo.latest_time_utc = AsyncMock(return_value=None)
        repo.upsert_rows = AsyncMock(return_value=10)
        repo.session = MagicMock()
        repo.session.commit = AsyncMock(side_effect=RuntimeError("db error"))
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

        svc = DailyCandleSyncService(
            repository=repo,
            kis_kr_fetcher=kis_fetcher,
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=AsyncMock(),
            upbit_crypto_fetcher=AsyncMock(),
        )

        with pytest.raises(RuntimeError, match="db error"):
            await svc.sync_one(
                target=SyncTarget(
                    market=MarketKey.KR, symbol="005930", partition="KRX"
                ),
                horizon_bars=400,
            )

        repo.upsert_rows.assert_awaited_once()  # upsert ran
        repo.session.commit.assert_awaited_once()  # commit attempted
        repo.session.rollback.assert_awaited_once()  # rollback called


@pytest.mark.asyncio
async def test_service_close_awaits_callbacks():
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    close_callback = AsyncMock()

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        close_callbacks=[close_callback],
    )

    await svc.close()

    close_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_kr_empty_kis_falls_back_to_toss_daily():
    from app.services.daily_candles.repository import MarketKey
    from app.services.daily_candles.sync_service import (
        DailyCandleSyncService,
        SyncTarget,
    )

    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(return_value=pd.DataFrame()),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        toss_kr_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [{"date": "2026-06-12", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "value": 20}]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss"


@pytest.mark.asyncio
async def test_us_empty_kis_and_yahoo_falls_back_to_toss_daily():
    from app.services.daily_candles.repository import MarketKey
    from app.services.daily_candles.sync_service import (
        DailyCandleSyncService,
        SyncTarget,
    )

    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(return_value=pd.DataFrame()),
        yahoo_us_fetcher=AsyncMock(return_value=[]),
        upbit_crypto_fetcher=AsyncMock(),
        toss_us_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [{"date": "2026-06-12", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "value": 20}]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss_fallback"


