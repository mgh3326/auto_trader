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
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.symbol import to_db_symbol
from app.services.daily_candles.converters import frame_to_rows
from app.services.daily_candles.crypto_identity import upbit_daily_candle_partition
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


KISKrFetcher = Callable[..., Awaitable["pd.DataFrame"]]
KISUsFetcher = Callable[..., Awaitable["pd.DataFrame"]]
YahooUsFetcher = Callable[..., Awaitable[list[YahooFallbackRow]]]
UpbitCryptoFetcher = Callable[..., Awaitable["pd.DataFrame"]]
TossDailyFetcher = Callable[..., Awaitable["pd.DataFrame"]]
# (symbol, exchange-or-None) pairs of currently held US symbols; see the
# holdings-union docstring on ``sync_market_universe``.
UsHoldingsLoader = Callable[[], Awaitable[list[tuple[str, str | None]]]]


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
        us_holdings_loader: UsHoldingsLoader | None = None,
        close_callbacks: list[Callable[[], object]] | None = None,
    ) -> None:
        self._repository = repository
        self._kis_kr = kis_kr_fetcher
        self._kis_us = kis_us_fetcher
        self._yahoo_us = yahoo_us_fetcher
        self._upbit = upbit_crypto_fetcher
        self._toss_kr = toss_kr_fetcher
        self._toss_us = toss_us_fetcher
        self._us_holdings_loader = us_holdings_loader
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
        fallback_used = False
        rows: list[DailyCandleRow] = []
        kis_errored = False
        try:
            frame = await self._kis_kr(code=target.symbol, n=horizon_bars)
            rows = frame_to_rows(
                frame, symbol=target.symbol, partition=target.partition, source="kis"
            )
        except Exception as exc:
            # ROB-706: a KIS exception/timeout is as much a fallback trigger as
            # empty rows (the 2026-07-04 maintenance RAISED, it did not return []).
            # Fall back only when a Toss fetcher is wired; otherwise preserve
            # today's behavior and re-raise.
            if self._toss_kr is None:
                raise
            kis_errored = True
            logger.warning(
                "KIS raised for KR symbol=%s; attempting Toss fallback: %s",
                target.symbol,
                exc,
            )
        if not rows and self._toss_kr is not None:
            if not kis_errored:
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
        canonical_partition = upbit_daily_candle_partition(target.symbol)
        if target.partition != canonical_partition:
            raise ValueError(
                "Crypto daily-candle target partition must match its canonical "
                f"Upbit symbol identity: symbol={target.symbol!r}, "
                f"partition={target.partition!r}, expected={canonical_partition!r}"
            )
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
        failed_symbols: list[dict[str, str]] = []
        for target in targets:
            try:
                result = await self.sync_one(target=target, horizon_bars=horizon_bars)
            except (InterfaceError, OperationalError):
                # A disconnected database is job-wide infrastructure failure,
                # not a symbol-local candle failure. Let the entrypoint report
                # its existing status=failed result.
                await self._repository.session.rollback()
                raise
            except Exception as exc:
                if market != "crypto":
                    raise
                # A failed statement can leave the shared session unusable for
                # the next symbol. Seeds are committed before this loop, so
                # this rollback cannot undo the newly created instruments.
                await self._repository.session.rollback()
                error_class = type(exc).__name__
                logger.warning(
                    "Crypto daily-candle sync failed symbol=%s error_class=%s",
                    target.symbol,
                    error_class,
                )
                failed_symbols.append(
                    {"symbol": target.symbol, "error_class": error_class}
                )
                continue
            rows_total += result.rows_upserted
            if result.fallback_used:
                fallback_count += 1
            if result.skipped_reason:
                skipped += 1
        failed_count = len(failed_symbols)
        status = (
            "failed"
            if targets and failed_count == len(targets)
            else "partial"
            if failed_count
            else "ok"
        )
        return {
            "status": status,
            "market": market,
            "targets_total": len(targets),
            "rows_upserted": rows_total,
            "fallback_count": fallback_count,
            "skipped": skipped,
            "failed_symbols": failed_symbols,
            "failed_count": failed_count,
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
            exchanges = {str(row.symbol): str(row.exchange) for row in result}
            if self._us_holdings_loader is not None:
                for raw_symbol, raw_exchange in await self._us_holdings_loader():
                    symbol = to_db_symbol(str(raw_symbol or "").strip().upper())
                    if not symbol or symbol in exchanges:
                        continue
                    exchange = str(raw_exchange or "").strip().upper()
                    if exchange not in {"NASD", "NYSE", "AMEX"}:
                        # A held symbol missing from the active set may still be
                        # registered (inactive) in the universe; read its
                        # recorded exchange directly — the active-only resolver
                        # could never succeed here by construction.
                        try:
                            lookup = await session.execute(
                                text(
                                    "SELECT exchange FROM public.us_symbol_universe"
                                    " WHERE symbol = :symbol"
                                ),
                                {"symbol": symbol},
                            )
                        except Exception:
                            await session.rollback()
                            logger.warning(
                                "US daily sync: held symbol %s exchange lookup "
                                "failed; skipping",
                                symbol,
                            )
                            continue
                        row = lookup.first()
                        if row is None:
                            logger.warning(
                                "US daily sync: held symbol %s has no resolvable "
                                "exchange; skipping",
                                symbol,
                            )
                            continue
                        exchange = str(row.exchange)
                    exchanges[symbol] = exchange
            return [
                SyncTarget(market=MarketKey.US, symbol=symbol, partition=exchange)
                for symbol, exchange in sorted(exchanges.items())
            ]
        if market == "crypto":
            sql = text(
                "SELECT market FROM public.upbit_symbol_universe"
                " WHERE is_active = TRUE AND quote_currency = 'KRW'"
                " ORDER BY market"
            )
            result = await session.execute(sql)
            targets = [
                SyncTarget(
                    market=MarketKey.CRYPTO,
                    symbol=row.market,
                    partition=upbit_daily_candle_partition(row.market),
                )
                for row in result
            ]
            await self._seed_upbit_krw_instruments(targets)
            return targets
        raise ValueError(f"Unknown market: {market}")

    async def _seed_upbit_krw_instruments(self, targets: list[SyncTarget]) -> None:
        """Persist missing Upbit KRW spot identities before candle writes.

        The daily-candle repository deliberately remains fail-closed when an
        instrument is absent. This producer-side, idempotent seed makes that
        final guard unnecessary for active KRW universe rows without weakening
        the repository contract for any other caller.
        """
        if not targets:
            return

        from sqlalchemy import text

        await self._repository.session.execute(
            text(
                "INSERT INTO public.crypto_instruments "
                "(venue, product, venue_symbol, base_asset, quote_asset, status) "
                "VALUES (:venue, :product, :venue_symbol, :base_asset, :quote_asset, :status) "
                "ON CONFLICT (venue, product, venue_symbol) DO NOTHING"
            ),
            [
                {
                    "venue": "upbit",
                    "product": "spot",
                    "venue_symbol": target.symbol,
                    "base_asset": target.symbol[4:],
                    "quote_asset": "KRW",
                    "status": "active",
                }
                for target in targets
            ],
        )
        # Commit before the per-symbol loop. A later fetch/write failure rolls
        # back its own transaction so the seed must already be durable.
        await self._commit_or_rollback()

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

    async def _us_holdings() -> list[tuple[str, str | None]]:
        """Held US symbols for the target union (KIS + manual holdings).

        Each pair is ``(raw_symbol, exchange_or_None)``; ``exchange_or_None``
        comes from the KIS holdings row (``ovrs_excg_cd``) and falls back to
        the universe lookup in ``_resolve_universe``. A failed loader leg is
        additive-only: the universe targets still sync.
        """
        from app.models.manual_holdings import MarketType
        from app.services.manual_holdings_service import ManualHoldingsService

        pairs: list[tuple[str, str | None]] = []
        try:
            for row in await kis.fetch_my_us_stocks():
                pairs.append(
                    (
                        str(row.get("ovrs_pdno") or ""),
                        row.get("ovrs_excg_cd"),
                    )
                )
        except Exception:
            logger.warning(
                "US daily sync: KIS holdings fetch failed; universe targets only",
                exc_info=True,
            )
        try:
            manual = await ManualHoldingsService(session).get_holdings_by_user(
                user_id=1, market_type=MarketType.US
            )
            pairs.extend((str(holding.ticker or ""), None) for holding in manual)
        except Exception:
            # A failed statement aborts the shared transaction; roll back so
            # the per-symbol exchange lookups in _resolve_universe still work.
            await session.rollback()
            logger.warning(
                "US daily sync: manual holdings fetch failed; continuing",
                exc_info=True,
            )
        return pairs

    return DailyCandleSyncService(
        repository=DailyCandlesRepository(session=session),
        kis_kr_fetcher=_kr,
        kis_us_fetcher=_us,
        yahoo_us_fetcher=_yahoo,
        upbit_crypto_fetcher=_upbit,
        toss_kr_fetcher=_toss if settings.toss_api_enabled else None,
        toss_us_fetcher=_toss if settings.toss_api_enabled else None,
        us_holdings_loader=_us_holdings,
        close_callbacks=[session.close, kis.close],
    )
