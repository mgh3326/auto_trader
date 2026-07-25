from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.us_symbol_universe import USSymbolUniverse
from app.services.us_symbol_universe_service import get_us_names_by_symbols


async def _session_with(rows):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: USSymbolUniverse.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    session.add_all(rows)
    await session.commit()
    return session


@pytest.mark.asyncio
async def test_get_us_names_prefers_korean_then_english_and_canonicalizes() -> None:
    rows = [
        USSymbolUniverse(
            symbol="TSLA",
            exchange="NASDAQ",
            name_kr="테슬라",
            name_en="Tesla",
            is_active=True,
        ),
        USSymbolUniverse(
            symbol="BRK.B",
            exchange="NASDAQ",
            name_kr="",
            name_en="Berkshire Hathaway",
            is_active=True,
        ),
        USSymbolUniverse(
            symbol="QQQ", exchange="NASDAQ", name_kr="", name_en="", is_active=True
        ),
        USSymbolUniverse(
            symbol="DEAD",
            exchange="NASDAQ",
            name_kr="옛이름",
            name_en="Old",
            is_active=False,
        ),
    ]
    session = await _session_with(rows)
    try:
        # caller passes the ledger's symbol form; BRK-B must canonicalize to BRK.B
        out = await get_us_names_by_symbols(["TSLA", "BRK-B", "QQQ", "DEAD"], session)
    finally:
        await session.close()

    assert out["TSLA"] == "테슬라"
    assert out["BRK-B"] == "Berkshire Hathaway"  # keyed by caller's original string
    assert "QQQ" not in out  # no usable name -> omitted
    assert "DEAD" not in out  # inactive -> omitted
