from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_home import (
    AssetCategoryLiteral,
    AssetTypeLiteral,
    CurrencyLiteral,
)


class SymbolNotFound(LookupError):
    """Raised when a stock-detail symbol cannot be resolved from active universes."""


@dataclass(frozen=True)
class ResolvedSymbol:
    symbol_db: str
    display_name: str
    exchange: str
    instrument_type: str
    asset_type: AssetTypeLiteral
    asset_category: AssetCategoryLiteral
    currency: CurrencyLiteral


def _normalize_crypto_market(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    if symbol.startswith("KRW-"):
        return symbol
    if symbol.endswith("-KRW"):
        base = symbol.removesuffix("-KRW")
        return f"KRW-{base}"
    if "-" not in symbol:
        return f"KRW-{symbol}"
    return symbol


async def resolve_symbol(
    market: NewsMarket,
    raw_symbol: str,
    db: AsyncSession,
) -> ResolvedSymbol:
    if market == "kr":
        symbol = raw_symbol.strip().upper()
        result = await db.execute(
            select(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == symbol,
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise SymbolNotFound(f"KR symbol not found: {raw_symbol}")
        return ResolvedSymbol(
            symbol_db=row.symbol,
            display_name=row.name,
            exchange=row.exchange,
            instrument_type="equity_kr",
            asset_type="equity",
            asset_category="kr_stock",
            currency="KRW",
        )

    if market == "us":
        symbol = to_db_symbol(raw_symbol.strip().upper())
        result = await db.execute(
            select(USSymbolUniverse).where(
                USSymbolUniverse.symbol == symbol,
                USSymbolUniverse.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise SymbolNotFound(f"US symbol not found: {raw_symbol}")
        return ResolvedSymbol(
            symbol_db=row.symbol,
            display_name=row.name_kr or row.name_en or row.symbol,
            exchange=row.exchange,
            instrument_type="equity_us",
            asset_type="equity",
            asset_category="us_stock",
            currency="USD",
        )

    if market == "crypto":
        symbol = _normalize_crypto_market(raw_symbol)
        result = await db.execute(
            select(UpbitSymbolUniverse).where(
                UpbitSymbolUniverse.market == symbol,
                UpbitSymbolUniverse.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise SymbolNotFound(f"Crypto symbol not found: {raw_symbol}")
        return ResolvedSymbol(
            symbol_db=row.market,
            display_name=row.korean_name or row.english_name or row.market,
            exchange=row.quote_currency,
            instrument_type="crypto",
            asset_type="crypto",
            asset_category="crypto",
            currency="KRW",
        )

    raise SymbolNotFound(f"Unsupported market: {market}")


__all__ = ["ResolvedSymbol", "SymbolNotFound", "resolve_symbol"]
