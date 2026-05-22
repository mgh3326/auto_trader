"""ROB-284 — crypto_instruments table contract."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_crypto_instruments_table_exists(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_instruments' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: (row.is_nullable, row.data_type) for row in result}
    assert "id" in cols
    assert "venue" in cols and cols["venue"][0] == "NO"
    assert "product" in cols and cols["product"][0] == "NO"
    assert "venue_symbol" in cols and cols["venue_symbol"][0] == "NO"
    assert "base_asset" in cols and cols["base_asset"][0] == "NO"
    assert "quote_asset" in cols and cols["quote_asset"][0] == "NO"
    assert "status" in cols and cols["status"][0] == "NO"
    for opt in (
        "precision_price",
        "precision_amount",
        "tick_size",
        "lot_size",
        "min_notional",
        "listed_at",
        "delisted_at",
        "metadata",
    ):
        assert opt in cols, f"missing optional column {opt}"


@pytest.mark.asyncio
async def test_crypto_instruments_unique_constraint(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO crypto_instruments "
            "(venue, product, venue_symbol, base_asset, quote_asset, status) "
            "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active')"
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES ('upbit', 'spot', 'KRW-BTC', 'BTC', 'KRW', 'active')"
            )
        )
        await db_session.flush()


@pytest.mark.asyncio
async def test_crypto_instruments_status_check(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES ('upbit', 'spot', 'KRW-XYZ', 'XYZ', 'KRW', 'bogus_status')"
            )
        )
        await db_session.flush()


@pytest.mark.asyncio
async def test_crypto_instrument_orm_roundtrip(db_session: AsyncSession) -> None:
    from app.models.crypto_instruments import CryptoInstrument

    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="active",
        precision_price=2,
        precision_amount=5,
        tick_size=0.01,
        lot_size=0.00001,
        min_notional=10,
    )
    db_session.add(inst)
    await db_session.flush()
    assert inst.id is not None
    fetched = await db_session.get(CryptoInstrument, inst.id)
    assert fetched is not None
    assert fetched.venue == "binance"
    assert fetched.extra_metadata is None
