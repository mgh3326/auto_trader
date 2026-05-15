"""Composition root for the Upbit public read-model cache."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from app.services.upbit_public_read_model.candles_cache import CandlesCache
from app.services.upbit_public_read_model.market_warnings import (
    MarketWarningsProvider,
    db_universe_warnings_provider,
)
from app.services.upbit_public_read_model.orderbook_cache import OrderbookCache
from app.services.upbit_public_read_model.ticker_cache import TickerCache
from app.services.upbit_public_read_model.trades_cache import TradesCache
from app.services.upbit_public_read_model.types import UpbitPublicSnapshot, _now_utc

_TRADES_CONCURRENCY = 4
_DEFAULT_READ_MODEL: UpbitPublicReadModel | None = None
_DEFAULT_REDIS_CLIENT: Any | None = None
_DEFAULT_READ_MODEL_LOCK = asyncio.Lock()


class UpbitPublicReadModel:
    def __init__(
        self,
        *,
        redis,
        ticker_fetcher: Callable[[list[str]], Awaitable[list[dict[str, Any]]]],
        orderbook_fetcher: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]],
        trades_fetcher: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
        warnings_provider: MarketWarningsProvider = db_universe_warnings_provider,
    ) -> None:
        self._ticker = TickerCache(redis=redis, fetcher=ticker_fetcher)
        self._orderbook = OrderbookCache(redis=redis, fetcher=orderbook_fetcher)
        self._trades = TradesCache(redis=redis, fetcher=trades_fetcher)
        self._candles = CandlesCache()
        self._warnings_provider = warnings_provider

    async def get_tickers(self, markets: Iterable[str]):
        return await self._ticker.get(list(markets))

    async def get_orderbooks(self, markets: Iterable[str]):
        return await self._orderbook.get(list(markets))

    async def get_recent_trades(self, market: str, count: int = 50):
        return await self._trades.get(market, count)

    async def get_candles(self, market: str, *, period, count: int):
        return await self._candles.get(market, period=period, count=count)

    async def get_market_warnings(self, markets: Iterable[str] | None = None):
        return await self._warnings_provider(list(markets) if markets else None)

    async def snapshot(
        self, markets: list[str], *, include_trades_for: list[str] | None = None
    ) -> UpbitPublicSnapshot:
        now = _now_utc()
        ticker, orderbook, warnings = await asyncio.gather(
            self._ticker.get(markets),
            self._orderbook.get(markets),
            self._warnings_provider(markets),
        )
        trades_block = None
        if include_trades_for:
            sem = asyncio.Semaphore(_TRADES_CONCURRENCY)

            async def one(market: str):
                async with sem:
                    return await self._trades.get(market, 50)

            trades_block = TradesCache.merge(
                await asyncio.gather(*(one(m) for m in include_trades_for))
            )
        sources = [ticker.meta, orderbook.meta, warnings.meta]
        if trades_block is not None:
            sources.append(trades_block.meta)
        return UpbitPublicSnapshot(
            asOf=now,
            ticker=ticker,
            orderbook=orderbook,
            trades=trades_block,
            marketWarnings=warnings,
            sources=sources,
        )


def _build_read_model(redis_client) -> UpbitPublicReadModel:
    from app.services.brokers.upbit.client import fetch_multiple_tickers
    from app.services.brokers.upbit.public_trades import fetch_recent_trades
    from app.services.upbit_orderbook import fetch_multiple_orderbooks

    return UpbitPublicReadModel(
        redis=redis_client,
        ticker_fetcher=fetch_multiple_tickers,
        orderbook_fetcher=fetch_multiple_orderbooks,
        trades_fetcher=fetch_recent_trades,
        warnings_provider=db_universe_warnings_provider,
    )


async def get_default_read_model(redis_client=None) -> UpbitPublicReadModel:
    """Return the shared default Upbit read model unless a custom client is injected."""
    if redis_client is not None:
        return _build_read_model(redis_client)

    global _DEFAULT_READ_MODEL, _DEFAULT_REDIS_CLIENT
    if _DEFAULT_READ_MODEL is None:
        async with _DEFAULT_READ_MODEL_LOCK:
            if _DEFAULT_READ_MODEL is None:
                from app.services.ohlcv_cache_common import create_redis_client

                _DEFAULT_REDIS_CLIENT = await create_redis_client()
                _DEFAULT_READ_MODEL = _build_read_model(_DEFAULT_REDIS_CLIENT)
    return _DEFAULT_READ_MODEL


async def close_default_read_model() -> None:
    """Close and reset the shared default Redis client/read model."""
    global _DEFAULT_READ_MODEL, _DEFAULT_REDIS_CLIENT
    redis_client = _DEFAULT_REDIS_CLIENT
    _DEFAULT_READ_MODEL = None
    _DEFAULT_REDIS_CLIENT = None
    if redis_client is None:
        return
    close = getattr(redis_client, "aclose", None) or getattr(
        redis_client, "close", None
    )
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        await result
