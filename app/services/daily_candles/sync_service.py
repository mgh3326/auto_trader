"""Daily candle sync orchestrator.

Composes:
- kis_daily_fetcher (KR + US primary)
- upbit daily fetcher (crypto primary)
- yahoo_us_fallback (US fallback only)
- DailyCandlesRepository (DB boundary)

Pure orchestration. The actual external-API calls live in the
fetcher modules; the SQL lives in the repository.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.daily_candles.converters import frame_to_rows
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.daily_candles.yahoo_us_fallback import YahooFallbackRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncTarget:
    market: MarketKey
    symbol: str
    partition: str  # exchange / venue / market


@dataclass(frozen=True, slots=True)
class SyncOneResult:
    target: SyncTarget
    rows_upserted: int
    fallback_used: bool
    skipped_reason: str | None = None


KISKrFetcher = Callable[..., Awaitable[pd.DataFrame]]
KISUsFetcher = Callable[..., Awaitable[pd.DataFrame]]
YahooUsFetcher = Callable[..., Awaitable[list[YahooFallbackRow]]]
UpbitCryptoFetcher = Callable[..., Awaitable[pd.DataFrame]]
TossDailyFetcher = Callable[..., Awaitable[pd.DataFrame]]


class DailyCandleSyncService:
    def __init__(
        self,
        *,
        repository: DailyCandlesRepository,
        kis_kr_fetcher: KISKrFetcher,
        kis_us_fetcher: KISUsFetcher,
        yahoo_us_fetcher: YahooUsFetcher,
        upbit_crypto_fetcher: UpbitCryptoFetcher,
        toss_kr_fetcher: TossDailyFetcher | None = None,
        toss_us_fetcher: TossDailyFetcher | None = None,
        close_callbacks: list[Callable[[], object]] | None = None,
    ) -> None:
        self._repository = repository
        self._kis_kr = kis_kr_fetcher
        self._kis_us = kis_us_fetcher
        self._yahoo_us = yahoo_us_fetcher
        self._upbit = upbit_crypto_fetcher
        self._toss_kr = toss_kr_fetcher
        self._toss_us = toss_us_fetcher
        self._close_callbacks = close_callbacks or []

    async def close(self) -> None:
        """Release resources owned by the default service factory."""
        for callback in self._close_callbacks:
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def sync_one(self, *, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        if target.market == MarketKey.KR:
            return await self._sync_kr(target, horizon_bars)
        if target.market == MarketKey.US:
            return await self._sync_us(target, horizon_bars)
        return await self._sync_crypto(target, horizon_bars)

    async def _sync_kr(self, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        frame = await self._kis_kr(code=target.symbol, n=horizon_bars)
        rows = frame_to_rows(
            frame, symbol=target.symbol, partition=target.partition, source="kis"
        )
        fallback_used = False
        if not rows and self._toss_kr is not None:
            logger.warning(
                "KIS returned no rows for KR symbol; attempting Toss fallback symbol=%s",
                target.symbol,
            )
            toss_frame = await self._toss_kr(symbol=target.symbol, n=horizon_bars)
            rows = frame_to_rows(
                toss_frame,
                symbol=target.symbol,
                partition=target.partition,
                source="toss",
            )
            fallback_used = True
        upserted = await self._repository.upsert_rows(market=target.market, rows=rows)
        await self._commit_or_rollback()
        return SyncOneResult(
            target=target, rows_upserted=upserted, fallback_used=fallback_used
        )

    async def _sync_us(self, target: SyncTarget, horizon_bars: int) -> SyncOneResult:
        frame = await self._kis_us(
            symbol=target.symbol, exchange_code=target.partition, n=horizon_bars
        )
        rows = frame_to_rows(
            frame, symbol=target.symbol, partition=target.partition, source="kis"
        )
        if rows:
            upserted = await self._repository.upsert_rows(
                market=target.market, rows=rows
            )
            await self._commit_or_rollback()
            return SyncOneResult(
                target=target, rows_upserted=upserted, fallback_used=False
            )

        logger.warning(
            "KIS returned no rows for US symbol; attempting Yahoo fallback symbol=%s exchange=%s",
            target.symbol,
            target.partition,
        )
        fallback_rows = await self._yahoo_us(symbol=target.symbol, n=horizon_bars)
        if not fallback_rows:
            if self._toss_us is not None:
                logger.warning(
                    "Yahoo returned no rows for US symbol; attempting Toss fallback symbol=%s exchange=%s",
                    target.symbol,
                    target.partition,
                )
                toss_frame = await self._toss_us(symbol=target.symbol, n=horizon_bars)
                repo_rows = frame_to_rows(
                    toss_frame,
                    symbol=target.symbol,
                    partition=target.partition,
                    source="toss_fallback",
                )
                if repo_rows:
                    upserted = await self._repository.upsert_rows(
                        market=target.market, rows=repo_rows
                    )
                    await self._commit_or_rollback()
                    return SyncOneResult(
                        target=target, rows_upserted=upserted, fallback_used=True
                    )
            return SyncOneResult(
                target=target,
                rows_upserted=0,
                fallback_used=True,
                skipped_reason="both_sources_empty",
            )
        repo_rows = [
            DailyCandleRow(
                time_utc=r.time_utc,
                symbol=r.symbol,
                partition=target.partition,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                adj_close=r.adj_close,
                volume=r.volume,
                value=r.value,
                source="yahoo_fallback",
            )
            for r in fallback_rows
        ]
        upserted = await self._repository.upsert_rows(
            market=target.market, rows=repo_rows
        )
        await self._commit_or_rollback()
        return SyncOneResult(target=target, rows_upserted=upserted, fallback_used=True)

    async def _sync_crypto(
        self, target: SyncTarget, horizon_bars: int
    ) -> SyncOneResult:
        frame = await self._upbit(market=target.symbol, days=horizon_bars)
        rows = frame_to_rows(
            frame, symbol=target.symbol, partition=target.partition, source="upbit"
        )
        upserted = await self._repository.upsert_rows(market=target.market, rows=rows)
        await self._commit_or_rollback()
        return SyncOneResult(target=target, rows_upserted=upserted, fallback_used=False)

    async def sync_market_universe(
        self, *, market: str, horizon_bars: int
    ) -> dict[str, Any]:
        """Run sync_one for every active (symbol, partition) pair in the market.

        Target universe rules (resolution helpers wire to existing services):
        - kr: union of active rows from kr_symbol_universe + KIS KR holdings + manual KR holdings.
        - us: union of active rows from us_symbol_universe + KIS US holdings + manual US holdings.
        - crypto: rows from upbit_symbol_universe (KRW market).

        For the holdings-union pattern, see app/services/us_candles_sync_service.py:471-481.
        """
        targets = await self._resolve_universe(market=market)
        rows_total = 0
        fallback_count = 0
        skipped = 0
        for target in targets:
            result = await self.sync_one(target=target, horizon_bars=horizon_bars)
            rows_total += result.rows_upserted
            if result.fallback_used:
                fallback_count += 1
            if result.skipped_reason:
                skipped += 1
        return {
            "market": market,
            "targets_total": len(targets),
            "rows_upserted": rows_total,
            "fallback_count": fallback_count,
            "skipped": skipped,
        }

    async def _resolve_universe(self, *, market: str) -> list[SyncTarget]:
        """Return list of (market, symbol, partition) targets to sync.

        Wires to the existing universe services. Implementation reads
        the per-market universe tables directly via the repository's
        session (we get it from the repository which already holds an
        AsyncSession).
        """
        from sqlalchemy import text

        session = self._repository.session

        if market == "kr":
            sql = text(
                "SELECT symbol FROM public.kr_symbol_universe"
                " WHERE is_active = TRUE ORDER BY symbol"
            )
            result = await session.execute(sql)
            return [
                SyncTarget(market=MarketKey.KR, symbol=row.symbol, partition="KRX")
                for row in result
            ]
        if market == "us":
            sql = text(
                "SELECT symbol, exchange FROM public.us_symbol_universe"
                " WHERE is_active = TRUE ORDER BY symbol"
            )
            result = await session.execute(sql)
            return [
                SyncTarget(
                    market=MarketKey.US, symbol=row.symbol, partition=row.exchange
                )
                for row in result
            ]
        if market == "crypto":
            sql = text(
                "SELECT market FROM public.upbit_symbol_universe"
                " WHERE is_active = TRUE AND quote_currency = 'KRW'"
                " ORDER BY market"
            )
            result = await session.execute(sql)
            return [
                SyncTarget(market=MarketKey.CRYPTO, symbol=row.market, partition="KRW")
                for row in result
            ]
        raise ValueError(f"Unknown market: {market}")

    async def _commit_or_rollback(self) -> None:
        """Commit the repository session after a successful upsert; rollback on error.

        The session is owned by the repository and exposed via its public
        ``session`` property.
        """
        session = self._repository.session
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _build_default_service() -> DailyCandleSyncService:
    """Build a DailyCandleSyncService wired to real dependencies.

    The repository receives a fresh AsyncSession from AsyncSessionLocal —
    the caller is responsible for closing/committing the session, but in
    cron / CLI contexts the service lives only for the duration of one
    invocation, so this is acceptable.
    """
    import app.services.brokers.upbit.client as upbit_service
    from app.core.config import settings
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kis.client import KISClient
    from app.services.daily_candles.kis_daily_fetcher import (
        fetch_kr_daily_unclamped,
        fetch_us_daily_unclamped,
    )
    from app.services.daily_candles.toss_daily_fetcher import fetch_daily_toss_unclamped
    from app.services.daily_candles.yahoo_us_fallback import (
        fetch_us_daily_yahoo_fallback,
    )

    session = AsyncSessionLocal()
    kis = KISClient()

    async def _kr(*, code: str, n: int) -> pd.DataFrame:
        return await fetch_kr_daily_unclamped(kis=kis, code=code, n=n)

    async def _us(*, symbol: str, exchange_code: str, n: int) -> pd.DataFrame:
        return await fetch_us_daily_unclamped(
            kis=kis, symbol=symbol, exchange_code=exchange_code, n=n
        )

    async def _yahoo(*, symbol: str, n: int) -> list[YahooFallbackRow]:
        return await fetch_us_daily_yahoo_fallback(symbol=symbol, n=n)

    async def _upbit(*, market: str, days: int) -> pd.DataFrame:
        return await upbit_service.fetch_ohlcv(market=market, days=days, period="day")

    async def _toss(*, symbol: str, n: int) -> pd.DataFrame:
        return await fetch_daily_toss_unclamped(symbol=symbol, n=n)

    return DailyCandleSyncService(
        repository=DailyCandlesRepository(session=session),
        kis_kr_fetcher=_kr,
        kis_us_fetcher=_us,
        yahoo_us_fetcher=_yahoo,
        upbit_crypto_fetcher=_upbit,
        toss_kr_fetcher=_toss if settings.toss_api_enabled else None,
        toss_us_fetcher=_toss if settings.toss_api_enabled else None,
        close_callbacks=[session.close, kis.close],
    )
