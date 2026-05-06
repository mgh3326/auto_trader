"""Read-only quote service for investment valuation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.kis.market_data import MarketDataClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from app.services.invest_home_readers import SafeKISClient

logger = logging.getLogger(__name__)


class InvestQuoteService:
    """Read-only 시세 helper. Toss/manual 보유 평가용."""

    def __init__(self, kis_client: SafeKISClient, db: AsyncSession):
        self._kis = kis_client
        self._db = db
        self._market_data = MarketDataClient(kis_client)

    async def fetch_kr_prices(self, symbols: list[str]) -> dict[str, float | None]:
        """Fetch current prices for Korean stocks."""
        if not symbols:
            return {}

        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                # inquire_price returns a DataFrame indexed by code
                # Using market="J" as per plan
                df = await self._market_data.inquire_price(symbol, market="J")
                if not df.empty:
                    results[symbol] = float(df.iloc[0]["close"])
                else:
                    results[symbol] = None
            except Exception as exc:
                logger.warning("Failed to fetch KR price for %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results

    async def fetch_us_prices(self, symbols: list[str]) -> dict[str, float | None]:
        """Fetch current prices for US stocks."""
        if not symbols:
            return {}

        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                # 1) get_us_exchange_by_symbol
                try:
                    exchange = await get_us_exchange_by_symbol(symbol, self._db)
                except Exception as exc:
                    logger.warning(
                        "Failed to resolve US exchange for %s: %s", symbol, exc
                    )
                    results[symbol] = None
                    return

                # 2) inquire_overseas_daily_price (n=1, period="D") as per plan
                df = await self._market_data.inquire_overseas_daily_price(
                    symbol, exchange_code=exchange, n=1, period="D"
                )
                if not df.empty:
                    results[symbol] = float(df.iloc[0]["close"])
                else:
                    results[symbol] = None
            except Exception as exc:
                logger.warning("Failed to fetch US price for %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results
