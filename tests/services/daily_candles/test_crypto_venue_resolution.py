"""ROB-1010 — Tests for crypto venue normalization, resolution, and forecast scoring."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trade_journal import forecast_service as svc


@pytest.mark.asyncio
async def test_crypto_instrument_id_resolution_with_upbit_venue(
    db_session: AsyncSession,
) -> None:
    """Verify resolve_crypto_instrument_ids resolves venue='upbit' for partition='upbit_krw'."""
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-ROBDOGE",
        base_asset="ROBDOGE",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    resolved = await repo.resolve_crypto_instrument_ids(
        symbols=["KRW-ROBDOGE"], partition="upbit_krw"
    )
    assert resolved == {"KRW-ROBDOGE": inst.id}

    rows = await repo.fetch_range(
        market=MarketKey.CRYPTO,
        symbol="KRW-ROBDOGE",
        partition="upbit_krw",
        start=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
    )
    # Instrument resolved, no candles yet -> []
    assert rows == []


@pytest.mark.asyncio
async def test_crypto_instrument_id_resolution_with_upbit_usdt_partition(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="USDT-ROBSOL",
        base_asset="ROBSOL",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    resolved = await repo.resolve_crypto_instrument_ids(
        symbols=["USDT-ROBSOL"], partition="upbit_usdt"
    )
    assert resolved == {"USDT-ROBSOL": inst.id}

    candle_time = dt.datetime(2026, 7, 29, tzinfo=dt.UTC)
    inserted = await repo.upsert_rows(
        market=MarketKey.CRYPTO,
        rows=[
            DailyCandleRow(
                time_utc=candle_time,
                symbol="USDT-ROBSOL",
                partition="upbit_usdt",
                open=100.0,
                high=110.0,
                low=90.0,
                close=105.0,
                adj_close=None,
                volume=12.0,
                value=1260.0,
                source="upbit",
            )
        ],
    )
    await db_session.flush()
    fetched = await repo.fetch_range(
        market=MarketKey.CRYPTO,
        symbol="USDT-ROBSOL",
        partition="upbit_usdt",
        start=candle_time,
        end=candle_time,
    )

    assert inserted == 1
    assert [row.symbol for row in fetched] == ["USDT-ROBSOL"]
    assert [row.partition for row in fetched] == ["upbit_usdt"]


@pytest.mark.asyncio
async def test_crypto_instrument_id_resolution_warning_when_venue_is_krw(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify legacy venue='KRW' returns empty map and logs diagnostic warning."""
    inst = CryptoInstrument(
        venue="KRW",
        product="spot",
        venue_symbol="KRW-ROBADA",
        base_asset="ROBADA",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    resolved = await repo.resolve_crypto_instrument_ids(
        symbols=["KRW-ROBADA"], partition="upbit_krw"
    )
    assert resolved == {}
    assert any(
        "incomplete results for venue='upbit'" in r.message for r in caplog.records
    )

    rows = await repo.fetch_range(
        market=MarketKey.CRYPTO,
        symbol="KRW-ROBADA",
        partition="upbit_krw",
        start=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
    )
    assert rows == []
    assert any(
        "fetch_range(market=CRYPTO) failed to resolve instrument_id" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_crypto_forecast_resolution_exits_unresolved_no_data(
    db_session: AsyncSession,
) -> None:
    """Verify forecast resolution yields unresolved_no_data on venue='KRW' and resolves cleanly when normalized to venue='upbit'."""
    unique_symbol = f"KRW-ROBFORECAST-{uuid.uuid4().hex[:8].upper()}"
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol=unique_symbol,
        base_asset=unique_symbol.replace("KRW-", ""),
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.commit()

    repo = DailyCandlesRepository(session=db_session)
    candle = DailyCandleRow(
        time_utc=dt.datetime(2026, 6, 2, 0, 0, tzinfo=dt.UTC),
        symbol=unique_symbol,
        partition="upbit_krw",
        open=90000000.0,
        high=105000000.0,
        low=89000000.0,
        close=100000000.0,
        adj_close=None,
        volume=10.0,
        value=1000000000.0,
        source="upbit",
    )
    await repo.upsert_rows(market=MarketKey.CRYPTO, rows=[candle])
    await db_session.commit()

    target = {
        "kind": "price_target",
        "target_price": 100000000.0,
        "direction": "at_or_above",
        "outcome_rule_version": "window-touch-v1-high-gte-low-lte",
    }
    _, row = await svc.save_forecast(
        db_session,
        created_by="claude",
        symbol=unique_symbol,
        instrument_type="crypto",
        forecast_target=target,
        probability=0.7,
        forecast_start_date="2026-06-01",
        review_date="2026-06-05",
    )
    await db_session.commit()

    # 1. Mutate venue to legacy mis-normalized 'KRW' -> DailyCandlesRepository fails to map instrument -> unresolved_no_data
    inst.venue = "KRW"
    await db_session.commit()

    unresolved_result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    assert unresolved_result["status"] == "unresolved_no_data"

    # 2. Normalize venue to "upbit" -> resolution succeeds with resolved status and closed_hit forecast status
    inst.venue = "upbit"
    await db_session.commit()

    resolved_result = await svc.resolve_forecast(
        db_session, forecast_id=str(row.forecast_id), persist=True
    )
    assert resolved_result["status"] == "resolved"
    assert resolved_result["changed"] is True
    assert resolved_result["forecast"]["status"] == "closed"


@pytest.mark.asyncio
async def test_normalize_crypto_venue_migration_sql(
    db_session: AsyncSession,
) -> None:
    """Simulate Alembic migration SQL updating venue='KRW' -> venue='upbit'."""
    inst = CryptoInstrument(
        venue="KRW",
        product="spot",
        venue_symbol="KRW-ROBSOL",
        base_asset="ROBSOL",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    await db_session.execute(
        text(
            """
            UPDATE crypto_instruments
            SET venue = 'upbit', updated_at = NOW()
            WHERE venue = 'KRW'
              AND NOT EXISTS (
                SELECT 1 FROM crypto_instruments target
                WHERE target.venue = 'upbit'
                  AND target.product = crypto_instruments.product
                  AND target.venue_symbol = crypto_instruments.venue_symbol
              )
            """
        )
    )
    await db_session.flush()
    await db_session.refresh(inst)

    assert inst.venue == "upbit"
