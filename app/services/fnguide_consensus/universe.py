"""Default collection universe: active KR stocks on KOSPI/KOSDAQ (hk 6508). Read-only.

The domain is six-digit codes, matching the CLI, the parser (``cmp_cd``), the model
and the table CHECK. Rows outside it are excluded here and counted, not silently
dropped, so the exclusion is visible in the run report.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse

UNIVERSE_EXCHANGES: tuple[str, ...] = ("KOSPI", "KOSDAQ")
SIX_DIGIT_PATTERN = "^[0-9]{6}$"


@dataclass(frozen=True)
class UniverseLoad:
    symbols: list[str]
    excluded_non_numeric: int


def _base_filters() -> tuple:
    return (
        KRSymbolUniverse.is_active.is_(True),
        KRSymbolUniverse.exchange.in_(UNIVERSE_EXCHANGES),
        KRSymbolUniverse.security_type == "STOCK",
    )


async def load_kr_stock_symbols(
    session: AsyncSession, limit: int | None = None
) -> UniverseLoad:
    six_digit = KRSymbolUniverse.symbol.op("~")(SIX_DIGIT_PATTERN)
    stmt = (
        select(KRSymbolUniverse.symbol)
        .where(*_base_filters(), six_digit)
        .order_by(KRSymbolUniverse.symbol)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    symbols = list((await session.execute(stmt)).scalars().all())
    excluded_stmt = (
        select(func.count())
        .select_from(KRSymbolUniverse)
        .where(*_base_filters(), ~six_digit)
    )
    excluded = int((await session.execute(excluded_stmt)).scalar_one())
    return UniverseLoad(symbols=symbols, excluded_non_numeric=excluded)
