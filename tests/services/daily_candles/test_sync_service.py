"""ROB-1336 acceptance tests for crypto daily-candle sync."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.daily_candles.sync_service import DailyCandleSyncService, SyncTarget


def _candle_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-08-29",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 10,
                "value": 20,
            }
        ]
    )


@asynccontextmanager
async def _seed_universe(
    session: AsyncSession, markets: list[str]
) -> AsyncIterator[None]:
    await session.execute(
        text(
            "INSERT INTO upbit_symbol_universe "
            "(market, quote_currency, base_currency, korean_name, english_name, "
            "market_warning, is_active) "
            "VALUES (:market, 'KRW', :base_asset, 'ROB-1336', 'ROB-1336', 'NONE', TRUE)"
        ),
        [{"market": market, "base_asset": market[4:]} for market in markets],
    )
    await session.commit()
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(
            text(
                "DELETE FROM crypto_candles_1d WHERE instrument_id IN ("
                "SELECT id FROM crypto_instruments WHERE venue = 'upbit' "
                "AND product = 'spot' AND venue_symbol IN :markets)"
            ).bindparams(bindparam("markets", expanding=True)),
            {"markets": markets},
        )
        await session.execute(
            text(
                "DELETE FROM crypto_instruments WHERE venue = 'upbit' "
                "AND product = 'spot' AND venue_symbol IN :markets"
            ).bindparams(bindparam("markets", expanding=True)),
            {"markets": markets},
        )
        await session.execute(
            text(
                "DELETE FROM upbit_symbol_universe WHERE market IN :markets"
            ).bindparams(bindparam("markets", expanding=True)),
            {"markets": markets},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_crypto_sync_autoseeds_missing_universe_instruments_idempotently(
    db_session: AsyncSession,
) -> None:
    suffix = uuid.uuid4().hex[:8].upper()
    markets = [f"KRW-R1336A{suffix}", f"KRW-R1336B{suffix}"]
    async with _seed_universe(db_session, markets):
        service = DailyCandleSyncService(
            repository=DailyCandlesRepository(session=db_session),
            kis_kr_fetcher=AsyncMock(),
            kis_us_fetcher=AsyncMock(),
            yahoo_us_fetcher=AsyncMock(),
            upbit_crypto_fetcher=AsyncMock(side_effect=lambda **_: _candle_frame()),
        )

        first = await service.sync_market_universe(market="crypto", horizon_bars=1)
        second = await service.sync_market_universe(market="crypto", horizon_bars=1)

        assert first["status"] == "ok"
        assert first["failed_count"] == 0
        assert first["failed_symbols"] == []
        assert first["rows_upserted"] == 2
        assert second["rows_upserted"] >= 0
        assert service._upbit.await_count == 4
        instrument_count = await db_session.scalar(
            text(
                "SELECT count(*) FROM crypto_instruments WHERE venue = 'upbit' "
                "AND product = 'spot' AND venue_symbol IN :markets"
            ).bindparams(bindparam("markets", expanding=True)),
            {"markets": markets},
        )
        candle_count = await db_session.scalar(
            text(
                "SELECT count(*) FROM crypto_candles_1d WHERE instrument_id IN ("
                "SELECT id FROM crypto_instruments WHERE venue = 'upbit' "
                "AND product = 'spot' AND venue_symbol IN :markets)"
            ).bindparams(bindparam("markets", expanding=True)),
            {"markets": markets},
        )
        assert instrument_count == 2
        assert candle_count == 2


@pytest.mark.asyncio
async def test_crypto_sync_isolates_fetch_failure_and_reports_partial(caplog) -> None:
    repository = AsyncMock()
    repository.upsert_rows = AsyncMock(return_value=1)
    repository.session.commit = AsyncMock()
    repository.session.rollback = AsyncMock()
    service = DailyCandleSyncService(
        repository=repository,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(
            side_effect=[RuntimeError("upbit unavailable"), _candle_frame()]
        ),
    )
    service._resolve_universe = AsyncMock(
        return_value=[
            SyncTarget(MarketKey.CRYPTO, "KRW-FAIL", "upbit_krw"),
            SyncTarget(MarketKey.CRYPTO, "KRW-OK", "upbit_krw"),
        ]
    )

    result = await service.sync_market_universe(market="crypto", horizon_bars=1)

    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert result["failed_symbols"] == [
        {"symbol": "KRW-FAIL", "error_class": "RuntimeError"}
    ]
    assert result["rows_upserted"] == 1
    repository.upsert_rows.assert_awaited_once()
    repository.session.rollback.assert_awaited_once()
    assert any(
        record.levelname == "WARNING"
        and "symbol=KRW-FAIL error_class=RuntimeError" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_crypto_sync_reports_failed_when_every_symbol_fails() -> None:
    repository = AsyncMock()
    repository.session.rollback = AsyncMock()
    service = DailyCandleSyncService(
        repository=repository,
        kis_kr_fetcher=AsyncMock(),
        kis_us_fetcher=AsyncMock(),
        yahoo_us_fetcher=AsyncMock(),
        upbit_crypto_fetcher=AsyncMock(side_effect=TimeoutError("upbit unavailable")),
    )
    service._resolve_universe = AsyncMock(
        return_value=[SyncTarget(MarketKey.CRYPTO, "KRW-FAIL", "upbit_krw")]
    )

    result = await service.sync_market_universe(market="crypto", horizon_bars=1)

    assert result["status"] == "failed"
    assert result["failed_count"] == 1
    assert result["failed_symbols"] == [
        {"symbol": "KRW-FAIL", "error_class": "TimeoutError"}
    ]


@pytest.mark.asyncio
async def test_daily_candle_job_preserves_partial_sync_status(monkeypatch) -> None:
    from app.jobs import daily_candles

    service = AsyncMock()
    service.sync_market_universe = AsyncMock(
        return_value={"market": "crypto", "status": "partial"}
    )

    async def build_service() -> AsyncMock:
        return service

    monkeypatch.setattr(daily_candles, "_build_default_service", build_service)

    result = await daily_candles.run_daily_candles_sync("crypto")

    assert result["status"] == "partial"
    service.close.assert_awaited_once()
