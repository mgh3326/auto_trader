"""ROB-284 — crypto_candles_1m schema + hypertable + repository."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_table_columns(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_candles_1m' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row.is_nullable for row in result}
    assert cols["instrument_id"] == "NO"
    assert cols["time"] == "NO"
    assert cols["open"] == "NO"
    assert cols["high"] == "NO"
    assert cols["low"] == "NO"
    assert cols["close"] == "NO"
    assert cols["base_volume"] == "NO"
    assert cols["quote_volume"] == "YES"
    assert cols["trade_count"] == "YES"
    assert cols["vwap"] == "YES"
    assert cols["taker_buy_base_volume"] == "YES"
    assert cols["taker_buy_quote_volume"] == "YES"
    assert cols["is_closed"] == "NO"
    assert cols["source"] == "NO"
    assert cols["source_event_at"] == "YES"
    assert cols["ingested_at"] == "NO"


@pytest.mark.skip(
    reason="blocker: server-only validation — Timescale create_hypertable is "
    "not called by Base.metadata.create_all in the test DB. Hypertable "
    "registration is verified manually post-migration on real environments. "
    "See migration alembic/versions/<rev_b>_add_crypto_candles_1m.py "
    "for the create_hypertable invocation."
)
@pytest.mark.asyncio
async def test_hypertable_registered(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'crypto_candles_1m'"
        )
    )
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_ohlc_check_rejects_inconsistent(db_session: AsyncSession) -> None:
    # Pre-seed an instrument.
    await db_session.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active')"
        )
    )
    inst_id = (
        await db_session.execute(
            text("SELECT id FROM crypto_instruments WHERE venue_symbol='KRW-BTC'")
        )
    ).scalar_one()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO crypto_candles_1m "
                "(instrument_id, time, open, high, low, close, base_volume, "
                "is_closed, source) "
                "VALUES (:iid, '2026-05-20T00:00:00Z', 100, 50, 60, 70, 1, true, 'test')"
            ),
            {"iid": inst_id},
        )
        await db_session.flush()


@pytest.mark.asyncio
async def test_minute_repository_idempotent_upsert(db_session: AsyncSession) -> None:
    from app.models.crypto_instruments import CryptoInstrument
    from app.services.minute_candles.repository import (
        MinuteCandleRow,
        MinuteCandlesRepository,
    )

    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = MinuteCandlesRepository(session=db_session)
    row = MinuteCandleRow(
        instrument_id=inst.id,
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        open=100,
        high=105,
        low=99,
        close=103,
        base_volume=10,
        quote_volume=1030,
        is_closed=True,
        source="binance_sdk_ws",
    )
    await repo.upsert_rows(rows=[row])
    await repo.upsert_rows(rows=[row])
    # Idempotency assertion: after two upserts of an identical closed
    # candle from the same source, the table holds exactly one row. The
    # closed-candle-protection WHERE clause in the ON CONFLICT clause means
    # the second insert is a no-op.
    cnt = (
        await db_session.execute(
            text("SELECT count(*) FROM crypto_candles_1m WHERE instrument_id = :i"),
            {"i": inst.id},
        )
    ).scalar_one()
    assert cnt == 1


@pytest.mark.asyncio
async def test_cross_venue_same_bucket_coexistence(db_session: AsyncSession) -> None:
    """ROB-284 — 4 distinct instruments at same time bucket must not collide."""
    from app.models.crypto_instruments import CryptoInstrument
    from app.services.minute_candles.repository import (
        MinuteCandleRow,
        MinuteCandlesRepository,
    )

    instruments = [
        ("upbit", "spot", "KRW-BTC", "BTC", "KRW"),
        ("binance", "spot", "BTCUSDT", "BTC", "USDT"),
        ("binance", "usdm_futures", "BTCUSDT", "BTC", "USDT"),
        ("alpaca", "paper", "BTC/USD", "BTC", "USD"),
    ]
    ids = []
    for venue, product, sym, base, quote in instruments:
        inst = CryptoInstrument(
            venue=venue,
            product=product,
            venue_symbol=sym,
            base_asset=base,
            quote_asset=quote,
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        ids.append(inst.id)

    repo = MinuteCandlesRepository(session=db_session)
    t = dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc)
    rows = [
        MinuteCandleRow(
            instrument_id=i,
            time_utc=t,
            open=100,
            high=101,
            low=99,
            close=100,
            base_volume=1,
            is_closed=True,
            source="test",
        )
        for i in ids
    ]
    await repo.upsert_rows(rows=rows)
    # asyncpg executemany cannot return cumulative rowcount across the batch,
    # so we verify by querying the table directly — the assertion is that 4
    # rows coexist at the same bucket across distinct instrument_ids.
    inserted = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM crypto_candles_1m "
                "WHERE time = :t AND instrument_id = ANY(:ids)"
            ),
            {"t": t, "ids": ids},
        )
    ).scalar_one()
    assert inserted == 4
