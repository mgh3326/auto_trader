"""ROB-284 — DailyCandlesRepository crypto path writes via instrument_id."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_instruments import CryptoInstrument
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)


@pytest.mark.asyncio
async def test_crypto_upsert_writes_via_instrument_id(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-SOL",
        base_asset="SOL",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    row = DailyCandleRow(
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.UTC),
        symbol="KRW-SOL",
        partition="upbit_krw",
        open=100,
        high=110,
        low=95,
        close=105,
        adj_close=None,
        volume=12.5,
        value=1300,
        source="upbit",
    )
    await repo.upsert_rows(market=MarketKey.CRYPTO, rows=[row])

    result = await db_session.execute(
        text(
            "SELECT instrument_id, base_volume, quote_volume, is_closed, source "
            "FROM crypto_candles_1d WHERE instrument_id = :i"
        ),
        {"i": inst.id},
    )
    stored = result.one()
    assert stored.instrument_id == inst.id
    assert float(stored.base_volume) == 12.5
    assert float(stored.quote_volume) == 1300
    assert stored.is_closed is True
    assert stored.source == "upbit"


@pytest.mark.asyncio
async def test_crypto_upsert_raises_for_unknown_pair(
    db_session: AsyncSession,
) -> None:
    repo = DailyCandlesRepository(session=db_session)
    row = DailyCandleRow(
        time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.UTC),
        symbol="KRW-NEWCOIN",
        partition="upbit_krw",
        open=1,
        high=1,
        low=1,
        close=1,
        adj_close=None,
        volume=1,
        value=1,
        source="upbit",
    )
    with pytest.raises(LookupError):
        await repo.upsert_rows(market=MarketKey.CRYPTO, rows=[row])


@pytest.mark.asyncio
async def test_crypto_latest_time_utc_resolves_via_instrument(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-ETH",
        base_asset="ETH",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    rows = [
        DailyCandleRow(
            time_utc=dt.datetime(2026, 5, 18, tzinfo=dt.UTC),
            symbol="KRW-ETH",
            partition="upbit_krw",
            open=100,
            high=101,
            low=99,
            close=100,
            adj_close=None,
            volume=1,
            value=100,
            source="upbit",
        ),
        DailyCandleRow(
            time_utc=dt.datetime(2026, 5, 20, tzinfo=dt.UTC),
            symbol="KRW-ETH",
            partition="upbit_krw",
            open=100,
            high=101,
            low=99,
            close=100,
            adj_close=None,
            volume=1,
            value=100,
            source="upbit",
        ),
    ]
    await repo.upsert_rows(market=MarketKey.CRYPTO, rows=rows)

    latest = await repo.latest_time_utc(
        market=MarketKey.CRYPTO, symbol="KRW-ETH", partition="upbit_krw"
    )
    assert latest is not None
    assert latest.date() == dt.date(2026, 5, 20)


@pytest.mark.asyncio
async def test_crypto_fetch_recent_returns_rows_in_ascending_order(
    db_session: AsyncSession,
) -> None:
    inst = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-XRP",
        base_asset="XRP",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    repo = DailyCandlesRepository(session=db_session)
    rows = [
        DailyCandleRow(
            time_utc=dt.datetime(2026, 5, d, tzinfo=dt.UTC),
            symbol="KRW-XRP",
            partition="upbit_krw",
            open=100,
            high=101,
            low=99,
            close=100,
            adj_close=None,
            volume=1,
            value=100,
            source="upbit",
        )
        for d in (18, 19, 20)
    ]
    await repo.upsert_rows(market=MarketKey.CRYPTO, rows=rows)
    recent = await repo.fetch_recent(
        market=MarketKey.CRYPTO,
        symbol="KRW-XRP",
        partition="upbit_krw",
        count=10,
    )
    assert [r.time_utc.day for r in recent] == [18, 19, 20]
    assert all(r.symbol == "KRW-XRP" for r in recent)
    assert all(r.partition == "upbit_krw" for r in recent)


@pytest.mark.asyncio
async def test_resolve_crypto_instrument_ids_batches_symbols_in_one_select(
    db_session: AsyncSession,
) -> None:
    instruments = [
        CryptoInstrument(
            venue="upbit",
            product="spot",
            venue_symbol=symbol,
            base_asset=symbol.removeprefix("KRW-"),
            quote_asset="KRW",
            status="active",
        )
        for symbol in ("KRW-BTC", "KRW-ETH", "KRW-XRP")
    ]
    db_session.add_all(instruments)
    await db_session.flush()

    statements: list[str] = []
    engine = db_session.get_bind()

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        if "crypto_instruments" in statement and statement.lstrip().upper().startswith(
            "SELECT"
        ):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        resolved = await DailyCandlesRepository(
            session=db_session
        ).resolve_crypto_instrument_ids(
            symbols=["KRW-XRP", "KRW-BTC", "KRW-ETH", "KRW-BTC"],
            partition="upbit_krw",
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert resolved == {item.venue_symbol: item.id for item in instruments}
    assert len(statements) == 1
    assert " IN " in statements[0].upper()


@pytest.mark.asyncio
async def test_resolve_crypto_instrument_ids_returns_only_known_symbols(
    db_session: AsyncSession,
) -> None:
    known = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-SOL",
        base_asset="SOL",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(known)
    await db_session.flush()

    resolved = await DailyCandlesRepository(
        session=db_session
    ).resolve_crypto_instrument_ids(
        symbols=["KRW-SOL", "KRW-NOT-SEEDED"],
        partition="upbit_krw",
    )

    assert resolved == {"KRW-SOL": known.id}
