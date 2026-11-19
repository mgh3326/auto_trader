from datetime import UTC, datetime
from types import SimpleNamespace
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
async def test_crypto_universe_uses_canonical_upbit_partition() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(return_value=[SimpleNamespace(market="KRW-BTC")])
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )

    targets = await svc._resolve_universe(market="crypto")

    assert targets == [
        SyncTarget(
            market=MarketKey.CRYPTO,
            symbol="KRW-BTC",
            partition="upbit_krw",
        )
    ]


@pytest.mark.asyncio
async def test_crypto_sync_rejects_partition_that_disagrees_with_symbol() -> None:
    repo = MagicMock()
    repo.session = MagicMock()
    repo.upsert_rows = AsyncMock()
    upbit_fetcher = AsyncMock()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=upbit_fetcher,
    )

    with pytest.raises(ValueError, match="must match"):
        await svc.sync_one(
            target=SyncTarget(
                market=MarketKey.CRYPTO,
                symbol="USDT-ETH",
                partition="upbit_krw",
            ),
            horizon_bars=30,
        )

    upbit_fetcher.assert_not_awaited()
    repo.upsert_rows.assert_not_awaited()


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
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
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
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
            )
        ),
    )

    result = await svc.sync_one(
        target=SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        horizon_bars=1,
    )

    assert result.fallback_used is True
    assert upserted_rows[0].source == "toss_fallback"


@pytest.mark.asyncio
async def test_kr_kis_exception_falls_back_to_toss_daily():
    """ROB-706: a KIS exception (not just empty rows) triggers the KR Toss fallback."""
    upserted_rows = []

    class Repo:
        session = AsyncMock()

        async def upsert_rows(self, *, market, rows):
            upserted_rows.extend(rows)
            return len(rows)

    Repo.session.commit = AsyncMock()
    svc = DailyCandleSyncService(
        repository=Repo(),
        kis_kr_fetcher=AsyncMock(side_effect=TimeoutError("kis maintenance")),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        toss_kr_fetcher=AsyncMock(
            return_value=pd.DataFrame(
                [
                    {
                        "date": "2026-06-12",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 10,
                        "value": 20,
                    }
                ]
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
async def test_kr_kis_exception_without_toss_fetcher_reraises():
    """No Toss fetcher wired (TOSS_API_ENABLED off) → a KIS exception still
    propagates (today's behavior); no upsert/commit runs."""
    repo = MagicMock()
    repo.upsert_rows = AsyncMock()
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(side_effect=RuntimeError("kis down")),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        # no toss_kr_fetcher
    )

    with pytest.raises(RuntimeError, match="kis down"):
        await svc.sync_one(
            target=SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
            horizon_bars=1,
        )

    repo.upsert_rows.assert_not_awaited()
    repo.session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_universe_unions_held_symbols_with_resolved_exchange() -> None:
    """Held US symbols missing from the active set still join the daily targets."""

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

        def first(self):
            return self._rows[0] if self._rows else None

    repo = MagicMock()
    repo.session = MagicMock()
    repo.session.execute = AsyncMock(
        side_effect=[
            _Result([SimpleNamespace(symbol="AAPL", exchange="NASD")]),
            _Result([SimpleNamespace(exchange="NYSE")]),  # BNDX inactive row
            _Result([]),  # ORPH: no universe row at all
        ]
    )
    loader = AsyncMock(
        return_value=[
            ("AAPL", "NYSE"),  # already in universe: universe exchange wins
            ("sgov", "amex"),  # held ETF absent from universe: holdings exchange
            ("BNDX", None),  # registered but inactive: universe row resolves
            ("ORPH", None),  # unresolvable: skipped
        ]
    )
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        us_holdings_loader=loader,
    )

    targets = await svc._resolve_universe(market="us")

    assert targets == [
        SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        SyncTarget(market=MarketKey.US, symbol="BNDX", partition="NYSE"),
        SyncTarget(market=MarketKey.US, symbol="SGOV", partition="AMEX"),
    ]
    loader.assert_awaited_once()


def _one_day_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-11-18",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 10,
                "value": 20,
            }
        ]
    )


def _yahoo_row(symbol: str):
    from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

    return YahooFallbackRow(
        time_utc=datetime(2026, 11, 18, 20, tzinfo=UTC),
        symbol=symbol,
        open=1.0,
        high=2.0,
        low=1.0,
        close=2.0,
        adj_close=2.0,
        volume=10.0,
        value=20.0,
    )


def _repo_with_session():
    repo = MagicMock()
    repo.upsert_rows = AsyncMock(side_effect=lambda *, market, rows: len(rows))
    repo.session = MagicMock()
    repo.session.commit = AsyncMock()
    repo.session.rollback = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_us_symbol_without_history_is_isolated_and_others_commit(caplog):
    """ANT-style failure: one symbol raises at the fallback, the market goes on."""
    from app.services.daily_candles.sync_service import DailyCandleSyncService

    repo = _repo_with_session()

    async def yahoo(*, symbol, n):
        if symbol == "ANT":
            raise ValueError("ANT OHLCV not found")
        return [_yahoo_row(symbol)]

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(return_value=pd.DataFrame()),
        yahoo_us_fetcher=AsyncMock(side_effect=yahoo),
        upbit_crypto_fetcher=AsyncMock(),
    )
    svc._resolve_universe = AsyncMock(
        return_value=[
            SyncTarget(market=MarketKey.US, symbol="ANT", partition="NYSE"),
            SyncTarget(market=MarketKey.US, symbol="AAPL", partition="NASD"),
        ]
    )

    result = await svc.sync_market_universe(market="us", horizon_bars=1)

    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert result["failed_symbols"] == [{"symbol": "ANT", "error_class": "ValueError"}]
    assert result["rows_upserted"] == 1
    assert result["targets_total"] == 2
    repo.session.commit.assert_awaited_once()
    repo.session.rollback.assert_awaited_once()
    assert any(
        record.levelname == "WARNING"
        and "market=us symbol=ANT error_class=ValueError" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_kr_symbol_failure_is_isolated_and_others_commit():
    from app.services.daily_candles.sync_service import DailyCandleSyncService

    repo = _repo_with_session()

    async def kis(*, code, n):
        if code == "000000":
            raise RuntimeError("kis rejected symbol")
        return _one_day_frame()

    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(side_effect=kis),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
        # no toss_kr_fetcher: the KIS exception propagates to the loop
    )
    svc._resolve_universe = AsyncMock(
        return_value=[
            SyncTarget(market=MarketKey.KR, symbol="000000", partition="KRX"),
            SyncTarget(market=MarketKey.KR, symbol="005930", partition="KRX"),
        ]
    )

    result = await svc.sync_market_universe(market="kr", horizon_bars=1)

    assert result["status"] == "partial"
    assert result["failed_symbols"] == [
        {"symbol": "000000", "error_class": "RuntimeError"}
    ]
    assert result["failed_count"] == 1
    assert result["rows_upserted"] == 1
    repo.session.commit.assert_awaited_once()
    repo.session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_kr_and_us_report_failed_when_every_symbol_fails():
    from app.services.daily_candles.sync_service import DailyCandleSyncService

    repo = _repo_with_session()
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(side_effect=RuntimeError("kis down")),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )
    svc._resolve_universe = AsyncMock(
        return_value=[SyncTarget(market=MarketKey.KR, symbol="000000", partition="KRX")]
    )

    result = await svc.sync_market_universe(market="kr", horizon_bars=1)

    assert result["status"] == "failed"
    assert result["failed_count"] == 1
    repo.session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_database_connection_error_still_aborts_the_job(market: str) -> None:
    from sqlalchemy.exc import OperationalError

    from app.services.daily_candles.sync_service import DailyCandleSyncService

    repo = _repo_with_session()
    db_down = OperationalError("SELECT 1", {}, Exception("connection lost"))
    svc = DailyCandleSyncService(
        repository=repo,
        kis_kr_fetcher=AsyncMock(side_effect=db_down),
        kis_us_fetcher=AsyncMock(side_effect=db_down),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(),
    )
    target_market = MarketKey.KR if market == "kr" else MarketKey.US
    svc._resolve_universe = AsyncMock(
        return_value=[
            SyncTarget(market=target_market, symbol="A", partition="KRX"),
            SyncTarget(market=target_market, symbol="B", partition="NASD"),
        ]
    )

    with pytest.raises(OperationalError):
        await svc.sync_market_universe(market=market, horizon_bars=1)

    repo.session.rollback.assert_awaited_once()
    repo.session.commit.assert_not_awaited()
