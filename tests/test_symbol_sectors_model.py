from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector

_TEST_SYMBOL = "915000"  # 9-prefix 합성 심볼 (공유 test DB 격리 관례)


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key == "999278")
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_symbol_sector_roundtrip_and_universe_fk(db_session):
    sector = SymbolSector(
        market="kr", source="naver_upjong", source_key="999278",
        name_kr="반도체와반도체장비", name_en=None,
    )
    db_session.add(sector)
    await db_session.flush()

    db_session.add(
        KRSymbolUniverse(
            symbol=_TEST_SYMBOL, name="테스트반도체", exchange="KOSPI",
            is_active=True, sector_id=sector.id,
            sector_updated_at=dt.datetime(2026, 6, 11, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse.symbol, SymbolSector.name_kr)
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
    ).one()
    assert row.name_kr == "반도체와반도체장비"


@pytest.mark.asyncio
async def test_symbol_sector_unique_market_source_key(db_session):
    db_session.add(
        SymbolSector(market="kr", source="naver_upjong", source_key="999278",
                     name_kr="반도체와반도체장비")
    )
    await db_session.flush()
    db_session.add(
        SymbolSector(market="kr", source="naver_upjong", source_key="999278",
                     name_kr="중복")
    )
    with pytest.raises(sa.exc.IntegrityError):
        await db_session.flush()
    await db_session.rollback()
