from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.services.symbol_sectors_service import (
    assign_symbol_sector,
    get_or_create_sector,
)

_TEST_SYMBOL = "916000"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


@pytest.mark.asyncio
async def test_get_or_create_is_idempotent_and_tracks_rename(db_session):
    sid1 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체와반도체장비",
    )
    sid2 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체와반도체장비",
    )
    assert sid1 == sid2  # 동일 키 → 같은 id

    # 개명 추적: 같은 키, 새 이름 → 같은 id, name_kr 갱신
    sid3 = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999278", name_kr="반도체",
    )
    assert sid3 == sid1
    row = (
        await db_session.execute(
            sa.select(SymbolSector).where(SymbolSector.id == sid1)
        )
    ).scalar_one()
    assert row.name_kr == "반도체"


@pytest.mark.asyncio
async def test_get_or_create_rejects_unknown_market(db_session):
    with pytest.raises(ValueError):
        await get_or_create_sector(
            db_session, market="crypto", source="naver_upjong",
            source_key="9991", name_kr="x",
        )


@pytest.mark.asyncio
async def test_assign_updates_existing_symbol_and_ignores_missing(db_session):
    db_session.add(
        KRSymbolUniverse(
            symbol=_TEST_SYMBOL, name="테스트", exchange="KOSPI", is_active=True
        )
    )
    await db_session.flush()
    sid = await get_or_create_sector(
        db_session, market="kr", source="naver_upjong",
        source_key="999285", name_kr="방송과엔터테인먼트",
    )

    assert await assign_symbol_sector(
        db_session, market="kr", symbol=_TEST_SYMBOL, sector_id=sid
    ) is True
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _TEST_SYMBOL)
        )
    ).scalar_one()
    assert row.sector_id == sid
    assert row.sector_updated_at is not None

    # 미존재 심볼 → False, INSERT 없음 (universe 생성은 sync 책임)
    assert await assign_symbol_sector(
        db_session, market="kr", symbol="917999", sector_id=sid
    ) is False
