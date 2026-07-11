"""ROB-701/ROB-828 — shared Redis cache for Toss sellable quantities.

The Toss ``GET /api/v1/sellable-quantity`` endpoint is in the ORDER_INFO
rate-limit group (6 TPS / 3 TPS peak), so fanning it out per holding serializes
to ~N/6 s. Redis shares warm values across API, worker, and MCP processes;
batched reads avoid replacing the broker N+1 with a Redis N+1.

Opt-in callers (ROB-701 + ROB-810): the invest_home reader (/invest home &
account-panel) AND the MCP ``get_holdings`` path (default; bypass with
``fresh_sellable=True``). Display/advisory surfaces only — real sell sizing is
safe because order tools re-validate sellable at broker submit
(``orders_toss_variants``), and KIS/Upbit sell validation reads its own broker
live. Confirmed fills and successful sell place/cancel/modify calls invalidate
the affected symbol. Redis failures are fail-open. enabled=False => always miss.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import redis.asyncio as redis
from redis.exceptions import WatchError

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "toss:sellable:v1"


def _cache_key(symbol: str) -> str:
    return f"{_KEY_PREFIX}:{symbol.strip().upper()}"


def _generation_key(symbol: str) -> str:
    return f"{_cache_key(symbol)}:generation"


@dataclass(frozen=True)
class TossSellableCacheRead:
    values: list[Decimal | None]
    generations: dict[str, int]


class TossSellableCache:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        redis_client: Any,
        enabled: bool = True,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._redis = redis_client
        self._enabled = enabled

    async def read_many(self, symbols: Sequence[str]) -> TossSellableCacheRead:
        if not symbols:
            return TossSellableCacheRead(values=[], generations={})
        normalized = [symbol.strip().upper() for symbol in symbols]
        generations = dict.fromkeys(normalized, 0)
        if not self._enabled or self._ttl <= 0 or self._redis is None:
            return TossSellableCacheRead(
                values=[None] * len(symbols), generations=generations
            )
        keys = [
            key
            for symbol in normalized
            for key in (_cache_key(symbol), _generation_key(symbol))
        ]
        try:
            raw_values = await self._redis.mget(keys)
        except Exception as exc:  # noqa: BLE001 — cache must fail open
            logger.warning("Toss sellable cache MGET failed: %s", exc)
            return TossSellableCacheRead(
                values=[None] * len(symbols), generations=generations
            )

        values: list[Decimal | None] = []
        for index, symbol in enumerate(normalized):
            raw = raw_values[index * 2]
            raw_generation = raw_values[index * 2 + 1]
            try:
                generations[symbol] = int(raw_generation or 0)
            except (ValueError, TypeError):
                generations[symbol] = 0
            if raw is None:
                values.append(None)
                continue
            try:
                values.append(Decimal(str(raw)))
            except (ValueError, TypeError):
                values.append(None)
        return TossSellableCacheRead(values=values, generations=generations)

    async def get_many(self, symbols: Sequence[str]) -> list[Decimal | None]:
        return (await self.read_many(symbols)).values

    async def get(self, symbol: str) -> Decimal | None:
        return (await self.get_many([symbol]))[0]

    async def put_many(
        self,
        values: Mapping[str, Decimal],
        *,
        expected_generations: Mapping[str, int] | None = None,
    ) -> None:
        if not values or not self._enabled or self._ttl <= 0 or self._redis is None:
            return
        ttl_ms = max(1, int(self._ttl * 1000))
        try:
            if expected_generations is None:
                pipeline = self._redis.pipeline(transaction=False)
                for symbol, value in values.items():
                    pipeline.set(_cache_key(symbol), str(value), px=ttl_ms)
                await pipeline.execute()
                return

            normalized = {
                symbol.strip().upper(): value for symbol, value in values.items()
            }
            generation_keys = [_generation_key(symbol) for symbol in normalized]
            async with self._redis.pipeline(transaction=True) as pipeline:
                await pipeline.watch(*generation_keys)
                current_raw = await pipeline.mget(generation_keys)
                current_generations = {
                    symbol: int(raw or 0)
                    for symbol, raw in zip(normalized, current_raw, strict=True)
                }
                eligible_values = {
                    symbol: value
                    for symbol, value in normalized.items()
                    if current_generations[symbol]
                    == expected_generations.get(symbol, 0)
                }
                pipeline.multi()
                for symbol, value in eligible_values.items():
                    pipeline.set(_cache_key(symbol), str(value), px=ttl_ms)
                await pipeline.execute()
        except WatchError:
            logger.debug("Toss sellable cache SET skipped after invalidation race")
        except Exception as exc:  # noqa: BLE001 — cache must fail open
            logger.warning("Toss sellable cache pipeline SET failed: %s", exc)

    async def put(self, symbol: str, value: Decimal) -> None:
        await self.put_many({symbol: value})

    async def invalidate_many(self, symbols: Sequence[str]) -> None:
        if not self._enabled or self._redis is None:
            return
        normalized = list(
            dict.fromkeys(
                symbol.strip().upper() for symbol in symbols if symbol.strip()
            )
        )
        if not normalized:
            return
        keys = [_cache_key(symbol) for symbol in normalized]
        try:
            pipeline = self._redis.pipeline(transaction=True)
            for symbol in normalized:
                pipeline.incr(_generation_key(symbol))
            pipeline.delete(*keys)
            await pipeline.execute()
        except Exception as exc:  # noqa: BLE001 — invalidation is best-effort
            logger.warning("Toss sellable cache DEL failed: %s", exc)

    async def invalidate(self, symbol: str) -> None:
        await self.invalidate_many([symbol])

    async def clear(self) -> None:
        if self._redis is None:
            return
        try:
            keys = [key async for key in self._redis.scan_iter(f"{_KEY_PREFIX}:*")]
            if keys:
                await self._redis.delete(*keys)
        except Exception as exc:  # noqa: BLE001 — cache must fail open
            logger.warning("Toss sellable cache clear failed: %s", exc)


_shared_sellable_cache: TossSellableCache | None = None


def get_shared_sellable_cache() -> TossSellableCache:
    """Process-local port backed by Redis values shared across runtimes."""
    global _shared_sellable_cache
    if _shared_sellable_cache is None:
        redis_client = None
        try:
            redis_client = redis.from_url(
                settings.get_redis_url(),
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001 — cache must fail open
            logger.warning("Toss sellable cache Redis client init failed: %s", exc)
        _shared_sellable_cache = TossSellableCache(
            ttl_seconds=float(
                getattr(settings, "toss_sellable_cache_ttl_seconds", 600.0)
            ),
            redis_client=redis_client,
            enabled=bool(getattr(settings, "toss_sellable_cache_enabled", True)),
        )
    return _shared_sellable_cache


def reset_shared_sellable_cache() -> None:
    """Test hook: drop the process-global cache so suites start clean."""
    global _shared_sellable_cache
    _shared_sellable_cache = None
