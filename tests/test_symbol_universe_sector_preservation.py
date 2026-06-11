"""ROB-512 갭3: universe sync가 lazy-fill된 sector_id를 지우지 않음을 고정.

kr/us _apply_snapshot 둘 다 name/exchange(/nxt_eligible/name_kr/name_en)/is_active만
필드 단위로 갱신한다 — 통째 upsert로 바뀌면 이 테스트가 깨져서 알린다.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.kr_symbol_universe_service import (
    _apply_snapshot as kr_apply_snapshot,
)
from app.services.kr_symbol_universe_service import _UniverseRow as KRRow
from app.services.us_symbol_universe_service import (
    _apply_snapshot as us_apply_snapshot,
)
from app.services.us_symbol_universe_service import _UniverseRow as USRow

_KR_SYMBOL = "918000"
_US_SYMBOL = "ZZROBTST"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    async def _purge():
        await db_session.execute(
            sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _KR_SYMBOL)
        )
        await db_session.execute(
            sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol == _US_SYMBOL)
        )
        await db_session.execute(
            sa.delete(SymbolSector).where(SymbolSector.source_key.like("999%"))
        )
        await db_session.commit()

    await _purge()
    yield
    await _purge()


async def _make_sector(db_session, market: str) -> int:
    sector = SymbolSector(
        market=market,
        source="naver_upjong" if market == "kr" else "yfinance_industry",
        source_key="999300",
        name_kr="테스트업종",
        name_en="TestIndustry",
    )
    db_session.add(sector)
    await db_session.flush()
    return sector.id


@pytest.mark.asyncio
async def test_kr_sync_preserves_sector_id(db_session):
    sid = await _make_sector(db_session, "kr")
    db_session.add(
        KRSymbolUniverse(
            symbol=_KR_SYMBOL,
            name="옛이름",
            exchange="KOSPI",
            is_active=True,
            sector_id=sid,
        )
    )
    await db_session.flush()

    # 이름이 바뀐 snapshot으로 sync (해당 심볼만 포함하면 다른 행은 비활성화
    # 되지만, _clean이 우리 행만 만들었고 공유 DB의 타 행 비활성화는 같은
    # 세션 내 flush 후 rollback 가능하도록 commit하지 않는다)
    await kr_apply_snapshot(
        db_session,
        {
            _KR_SYMBOL: KRRow(
                symbol=_KR_SYMBOL, name="새이름", exchange="KOSPI", nxt_eligible=False
            )
        },
    )
    row = (
        await db_session.execute(
            sa.select(KRSymbolUniverse).where(KRSymbolUniverse.symbol == _KR_SYMBOL)
        )
    ).scalar_one()
    assert row.name == "새이름"
    assert row.sector_id == sid  # 보존!
    await db_session.rollback()  # 타 행 deactivation 원복


@pytest.mark.asyncio
async def test_us_sync_preserves_sector_id(db_session):
    sid = await _make_sector(db_session, "us")
    db_session.add(
        USSymbolUniverse(
            symbol=_US_SYMBOL,
            exchange="NASDAQ",
            name_kr="옛",
            name_en="Old",
            is_active=True,
            sector_id=sid,
        )
    )
    await db_session.flush()

    await us_apply_snapshot(
        db_session,
        {
            _US_SYMBOL: USRow(
                symbol=_US_SYMBOL, exchange="NASDAQ", name_kr="새", name_en="New"
            )
        },
    )
    row = (
        await db_session.execute(
            sa.select(USSymbolUniverse).where(USSymbolUniverse.symbol == _US_SYMBOL)
        )
    ).scalar_one()
    assert row.name_en == "New"
    assert row.sector_id == sid  # 보존!
    await db_session.rollback()
