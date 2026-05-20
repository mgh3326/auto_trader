"""ROB-285 — Binance kline → MinuteCandlesRepository ingest pipeline."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.ingest import BinanceCandleIngester
from app.services.brokers.binance.ws_client import KlineEvent


def _mk_event(
    *,
    symbol: str = "BTCUSDT",
    open_time: dt.datetime | None = None,
    is_closed: bool = True,
) -> KlineEvent:
    ot = open_time or dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    return KlineEvent(
        symbol=symbol,
        interval="1m",
        open_time=ot,
        close_time=ot + dt.timedelta(minutes=1) - dt.timedelta(milliseconds=1),
        open=Decimal("30000.0"),
        high=Decimal("30100.0"),
        low=Decimal("29900.0"),
        close=Decimal("30050.0"),
        base_volume=Decimal("12.5"),
        quote_volume=Decimal("375625.0"),
        trade_count=100,
        is_closed=is_closed,
    )


@pytest.mark.asyncio
async def test_ingest_closed_kline_persists_via_repository(
    db_session: AsyncSession,
) -> None:
    """A KlineEvent with is_closed=True is upserted into crypto_candles_1m
    via MinuteCandlesRepository, using a cached instrument_id lookup."""
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
    ingester = BinanceCandleIngester(session=db_session)
    persisted = await ingester.ingest(_mk_event(symbol="BTCUSDT"))
    assert persisted is True
    row = (
        await db_session.execute(
            text(
                "SELECT instrument_id, source FROM crypto_candles_1m "
                "WHERE instrument_id = :iid"
            ),
            {"iid": inst.id},
        )
    ).first()
    assert row is not None
    assert row[0] == inst.id
    assert row[1] == "binance_sdk_ws"


@pytest.mark.asyncio
async def test_ingest_skips_kline_for_unknown_symbol(
    db_session: AsyncSession, caplog
) -> None:
    """When no crypto_instruments row exists for (binance, spot, NEWCOIN),
    the ingest layer logs a WARNING and skips — does not auto-create."""
    ingester = BinanceCandleIngester(session=db_session)
    with caplog.at_level(logging.WARNING, logger="app.services.brokers.binance.ingest"):
        persisted = await ingester.ingest(_mk_event(symbol="NEWCOIN"))
    assert persisted is False
    assert any(
        "no crypto_instruments row" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_ingest_idempotent_upsert(db_session: AsyncSession) -> None:
    """Re-ingesting the same closed kline is a no-op at the DB level —
    one row exists for (instrument_id, time)."""
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
    ingester = BinanceCandleIngester(session=db_session)
    event = _mk_event(symbol="ETHUSDT")
    assert await ingester.ingest(event) is True
    assert await ingester.ingest(event) is True
    count = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM crypto_candles_1m "
                "WHERE instrument_id = :iid"
            ),
            {"iid": inst.id},
        )
    ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_ingest_drops_in_progress_kline_defensively(
    db_session: AsyncSession,
) -> None:
    """Even if a caller forgets to filter, the ingester drops non-closed klines."""
    ingester = BinanceCandleIngester(session=db_session)
    persisted = await ingester.ingest(_mk_event(symbol="BTCUSDT", is_closed=False))
    assert persisted is False


@pytest.mark.asyncio
async def test_ingest_caches_instrument_id_across_calls(
    db_session: AsyncSession,
) -> None:
    """Open items lean #7 (Task 13): in-memory cache for instrument_id."""
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="SOLUSDT",
        base_asset="SOL",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    ingester = BinanceCandleIngester(session=db_session)
    # First call populates the cache.
    await ingester.ingest(_mk_event(symbol="SOLUSDT"))
    assert "SOLUSDT" in ingester._cache  # noqa: SLF001 — testing internal cache
    cached_id = ingester._cache["SOLUSDT"]
    assert cached_id == inst.id
