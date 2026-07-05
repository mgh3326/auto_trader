"""Read-only quote service for investment valuation (ROB-696 fallback chain)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.brokers.kis.market_data import MarketDataClient
from app.services.brokers.toss.client import TossReadClient
from app.services.invest_price_fallback import (
    Fetcher,
    PriceFallbackResolver,
    fetch_toss_batch_prices,
)
from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from app.services.invest_home_readers import SafeKISClient

logger = logging.getLogger(__name__)


class InvestQuoteService:
    """Read-only 시세 helper with a fail-open KIS → Toss → snapshot chain."""

    def __init__(
        self,
        kis_client: SafeKISClient,
        db: AsyncSession,
        toss_client: TossReadClient | None = None,
    ) -> None:
        self._kis = kis_client
        self._db = db
        self._market_data = MarketDataClient(kis_client)
        self._toss_client = toss_client

    async def fetch_kr_prices(self, symbols: list[str]) -> dict[str, float | None]:
        return await self._resolve(symbols, market="kr", kis_fetch=self._kis_fetch_kr)

    async def fetch_us_prices(self, symbols: list[str]) -> dict[str, float | None]:
        return await self._resolve(symbols, market="us", kis_fetch=self._kis_fetch_us)

    async def _resolve(
        self, symbols: list[str], *, market: str, kis_fetch: Fetcher
    ) -> dict[str, float | None]:
        if not symbols:
            return {}
        toss_fetch, owned = self._build_toss_fetch()
        try:
            resolver = PriceFallbackResolver(
                kis_fetch=kis_fetch,
                toss_fetch=toss_fetch,
                snapshot_fetch=lambda syms: self._snapshot_latest(market, syms),
                market=market,
            )
            return await resolver.resolve(symbols)
        finally:
            if owned is not None:
                await owned.aclose()

    def _build_toss_fetch(self) -> tuple[Fetcher | None, TossReadClient | None]:
        if self._toss_client is not None:
            client = self._toss_client
            return (lambda syms: fetch_toss_batch_prices(client, syms), None)
        if bool(getattr(settings, "toss_api_enabled", False)):
            # Fail-open: enabled-but-misconfigured Toss makes from_settings()
            # raise TossMissingCredentials (auth.py:80,83,111). Since this runs
            # OUTSIDE the try/finally in _resolve, a raw raise would escape
            # fetch_kr_prices/fetch_us_prices — so guard it and skip the layer.
            try:
                client = TossReadClient.from_settings()
            except Exception as exc:  # noqa: BLE001 — fail-open, skip Toss layer
                logger.warning(
                    "invest price fallback: Toss client construction failed; "
                    "skipping Toss layer: %s",
                    exc,
                )
                return (None, None)
            return (lambda syms: fetch_toss_batch_prices(client, syms), client)
        return (None, None)

    async def _snapshot_latest(
        self, market: str, symbols: list[str]
    ) -> dict[str, float | None]:
        try:
            found = await MarketQuoteSnapshotsRepository(self._db).latest_prices(
                market, symbols
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, resolver -> None
            logger.warning("invest price snapshot read failed (%s): %s", market, exc)
            return {}
        return dict(found)

    async def _kis_fetch_kr(self, symbols: list[str]) -> dict[str, float | None]:
        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                df = await self._market_data.inquire_price(symbol, market="J")
                results[symbol] = float(df.iloc[0]["close"]) if not df.empty else None
            except Exception as exc:  # noqa: BLE001 — summarized by the resolver
                logger.debug("KIS KR price miss %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results

    async def _kis_fetch_us(self, symbols: list[str]) -> dict[str, float | None]:
        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                exchange = await get_us_exchange_by_symbol(symbol, self._db)
                # ROB-708: live last (HHDFS00000300), mirroring get_quote US, so
                # KIS-resolved US prices agree with Toss-resolved live-last prices
                # instead of silently mixing in a settled daily close (HHDFS76240000).
                # _build_overseas_price_frame returns empty when last is None/<=0,
                # so an empty frame -> None -> resolver falls through to Toss/snapshot.
                df = await self._market_data.inquire_overseas_price(
                    symbol, exchange_code=exchange
                )
                results[symbol] = float(df.iloc[0]["close"]) if not df.empty else None
            except Exception as exc:  # noqa: BLE001 — summarized by the resolver
                logger.debug("KIS US price miss %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results

    async def kis_only_kr_prices(self, symbols: list[str]) -> dict[str, float | None]:
        """ROB-709 shadow: RAW KIS KR batch layer (no fallback chain). Read-only."""
        return await self._kis_fetch_kr(symbols)

    async def kis_only_us_prices(self, symbols: list[str]) -> dict[str, float | None]:
        """ROB-709 shadow: RAW KIS US batch layer (no fallback chain). Read-only.

        NOTE (ROB-708 already landed on this branch): _kis_fetch_us now reads a
        live-last quote (inquire_overseas_price / HHDFS00000300), so the A/B
        shadow's US divergence bar is a valid promotion signal when this
        passthrough is used as the KIS side (pass --us-kis-live-last).
        """
        return await self._kis_fetch_us(symbols)
