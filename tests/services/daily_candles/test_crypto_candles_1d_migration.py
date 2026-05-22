"""ROB-284 — crypto_candles_1d in-place migration to instrument-FK shape.

The test DB schema is built by ``Base.metadata.create_all`` which produces
the post-migration (step 3) shape directly — it does not execute the three
alembic revisions sequentially. Intermediate-state assertions (legacy
columns still present after step 1; backfill correctness after step 2)
are therefore marked server-only and verified against a real PG +
TimescaleDB environment, not the in-process test DB.

What this test file DOES verify against the test DB:
- The final-shape schema produced by the ORM model
  (``app.models.crypto_candles.CryptoCandle1d``) matches what step 3 of
  the migration produces (column nullability, PK shape, CHECK constraints).
- The CryptoCandle1d ORM model accepts a valid insert via the session
  fixture.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# -----------------------------------------------------------------------------
# Step 1 + Step 2 intermediate-state assertions — server-only.
# -----------------------------------------------------------------------------


@pytest.mark.skip(
    reason="blocker: server-only validation — step 1 adds columns to the "
    "legacy table shape, but the test DB rebuilds crypto_candles_1d from "
    "the post-step-3 ORM model via Base.metadata.create_all. Intermediate "
    "states are validated on a real DB during the alembic upgrade run."
)
@pytest.mark.asyncio
async def test_step1_adds_nullable_columns(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1d'"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    # Old columns still present at this stage:
    assert "symbol" in cols
    assert "market" in cols
    # New columns added by step 1 — all nullable initially:
    assert cols.get("instrument_id") == "YES"
    assert cols.get("base_volume") == "YES"
    assert cols.get("quote_volume") == "YES"
    assert cols.get("is_closed") == "YES"
    assert cols.get("source_event_at") == "YES"


@pytest.mark.skip(
    reason="blocker: server-only validation — the backfill UPDATE runs "
    "against pre-existing legacy rows. The test DB starts the table from "
    "the ORM model and never holds legacy rows. Backfill correctness is "
    "validated on a real DB during the alembic upgrade run; the migration "
    "is also re-runnable and the step 3 NULL check fails closed if the "
    "backfill is incomplete."
)
@pytest.mark.asyncio
async def test_step2_backfill_populates_instrument_id_and_volumes(
    db_session: AsyncSession,
) -> None:
    rows_total = (
        await db_session.execute(text("SELECT count(*) FROM crypto_candles_1d"))
    ).scalar_one()
    rows_with_iid = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM crypto_candles_1d WHERE instrument_id IS NOT NULL"
            )
        )
    ).scalar_one()
    rows_with_base_vol = (
        await db_session.execute(
            text("SELECT count(*) FROM crypto_candles_1d WHERE base_volume IS NOT NULL")
        )
    ).scalar_one()
    rows_closed = (
        await db_session.execute(
            text("SELECT count(*) FROM crypto_candles_1d WHERE is_closed IS TRUE")
        )
    ).scalar_one()
    assert rows_total > 0, "Test database must contain crypto candle fixture rows"
    assert rows_with_iid == rows_total
    assert rows_with_base_vol == rows_total
    assert rows_closed == rows_total


@pytest.mark.skip(
    reason="blocker: server-only validation — depends on legacy "
    "(market, symbol) rows being present and translated by step 2."
)
@pytest.mark.asyncio
async def test_step2_creates_one_instrument_per_distinct_pair(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        text("SELECT COUNT(DISTINCT instrument_id) FROM crypto_candles_1d")
    )
    distinct_iids = result.scalar_one()
    result = await db_session.execute(
        text("SELECT count(*) FROM crypto_instruments WHERE venue = 'upbit'")
    )
    upbit_instruments = result.scalar_one()
    assert distinct_iids == upbit_instruments


# -----------------------------------------------------------------------------
# Step 3 final-shape assertions — verified against ORM-driven test DB.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step3_final_shape(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1d'"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    # Legacy columns are gone.
    assert "symbol" not in cols
    assert "market" not in cols
    assert "volume" not in cols
    assert "value" not in cols
    # New shape:
    assert cols["instrument_id"] == "NO"
    assert cols["base_volume"] == "NO"
    assert cols["is_closed"] == "NO"
    assert cols["quote_volume"] == "YES"


@pytest.mark.asyncio
async def test_step3_primary_key_is_instrument_time(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        text(
            "SELECT a.attname "
            "FROM pg_index i JOIN pg_attribute a "
            "  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'public.crypto_candles_1d'::regclass "
            "  AND i.indisprimary"
        )
    )
    pk_cols = {row.attname for row in result}
    assert pk_cols == {"instrument_id", "time"}


@pytest.mark.asyncio
async def test_step3_checks_present(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'public.crypto_candles_1d'::regclass "
            "  AND contype = 'c'"
        )
    )
    check_names = {row.conname for row in result}
    expected = {
        "ck_crypto_candles_1d_base_volume_nn",
        "ck_crypto_candles_1d_high_ge_low",
        "ck_crypto_candles_1d_high_ge_oc",
        "ck_crypto_candles_1d_low_le_oc",
    }
    assert expected <= check_names


@pytest.mark.asyncio
async def test_daily_candle_orm_roundtrip(db_session: AsyncSession) -> None:
    from app.models.crypto_candles import CryptoCandle1d
    from app.models.crypto_instruments import CryptoInstrument

    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    candle = CryptoCandle1d(
        instrument_id=inst.id,
        time=dt.datetime(2026, 5, 20, tzinfo=dt.UTC),
        open=3000,
        high=3100,
        low=2950,
        close=3050,
        base_volume=42.5,
        quote_volume=128000,
        is_closed=True,
        source="test",
    )
    db_session.add(candle)
    await db_session.flush()
    fetched = await db_session.get(
        CryptoCandle1d,
        (inst.id, dt.datetime(2026, 5, 20, tzinfo=dt.UTC)),
    )
    assert fetched is not None
    assert float(fetched.close) == 3050.0
