"""Default collection universe: active KR stocks on KOSPI/KOSDAQ (hk 6508). Read-only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse

UNIVERSE_EXCHANGES: tuple[str, ...] = ("KOSPI", "KOSDAQ")


async def load_kr_stock_symbols(
    session: AsyncSession, limit: int | None = None
) -> list[str]:
    stmt = (
        select(KRSymbolUniverse.symbol)
        .where(
            KRSymbolUniverse.is_active.is_(True),
            KRSymbolUniverse.exchange.in_(UNIVERSE_EXCHANGES),
            KRSymbolUniverse.security_type == "STOCK",
        )
        .order_by(KRSymbolUniverse.symbol)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())
