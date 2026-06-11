"""ROB-512 갭3: symbol_sectors 쓰기 전용 서비스.

모든 섹터 쓰기는 이 모듈의 두 함수만 사용한다. universe 행 INSERT는 하지
않는다(행 생성은 시장별 sync의 책임). 동시 enrichment의 중복 생성 경합은
ON CONFLICT DO NOTHING으로 흡수한다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.symbol_sectors import SymbolSector
from app.models.us_symbol_universe import USSymbolUniverse

logger = logging.getLogger(__name__)

_UNIVERSE_BY_MARKET = {"kr": KRSymbolUniverse, "us": USSymbolUniverse}


def _require_market(market: str) -> None:
    if market not in _UNIVERSE_BY_MARKET:
        raise ValueError(f"unsupported market for symbol sectors: {market!r}")


async def get_or_create_sector(
    db: AsyncSession,
    *,
    market: str,
    source: str,
    source_key: str,
    name_kr: str | None = None,
    name_en: str | None = None,
) -> int:
    """UNIQUE(market,source,source_key)로 get-or-create하고 id를 반환.

    기존 행의 이름이 새 값과 다르면 갱신한다(소스 측 개명 추적). None 인자는
    기존 값을 지우지 않는다.
    """
    _require_market(market)
    await db.execute(
        pg_insert(SymbolSector)
        .values(
            market=market,
            source=source,
            source_key=source_key,
            name_kr=name_kr,
            name_en=name_en,
        )
        .on_conflict_do_nothing(constraint="uq_symbol_sectors_market_source_key")
    )
    row = (
        await db.execute(
            sa.select(SymbolSector).where(
                SymbolSector.market == market,
                SymbolSector.source == source,
                SymbolSector.source_key == source_key,
            )
        )
    ).scalar_one()
    changed = False
    if name_kr is not None and row.name_kr != name_kr:
        row.name_kr = name_kr
        changed = True
    if name_en is not None and row.name_en != name_en:
        row.name_en = name_en
        changed = True
    if changed:
        await db.flush()
    return row.id


async def assign_symbol_sector(
    db: AsyncSession, *, market: str, symbol: str, sector_id: int
) -> bool:
    """universe 행의 sector_id/sector_updated_at만 갱신. 미존재 심볼은 False."""
    _require_market(market)
    model = _UNIVERSE_BY_MARKET[market]
    result = await db.execute(
        sa.update(model)
        .where(model.symbol == symbol)
        .values(sector_id=sector_id, sector_updated_at=datetime.now(UTC))
    )
    await db.flush()
    return bool(result.rowcount)
