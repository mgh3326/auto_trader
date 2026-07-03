"""ROB-688 — short-TTL fail-open Redis cache for get_sector_peers KR fetches.

Cache-aside over the Naver mobile /basic+/integration bundle (keyed by stock
code) and the sector detail page derivation (keyed by industry code). Mirrors
the fail-open contract of app.core.analyze_cache: any Redis outage or malformed
payload degrades to a live fetch and never raises. Gated by
settings.naver_peer_cache_enabled (forced off in tests/conftest.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

from app.core.config import settings
from app.services.ohlcv_cache_common import create_redis_client

logger = logging.getLogger(__name__)

_INTEG_PREFIX = "naver_peer:integ:"
_SECTOR_PREFIX = "naver_peer:sector:"
_REDIS_CLIENT: redis.Redis | None = None


async def _get_redis_client() -> redis.Redis | None:
    global _REDIS_CLIENT
    if not settings.naver_peer_cache_enabled:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        _REDIS_CLIENT = await create_redis_client()
    except Exception as exc:  # noqa: BLE001 — fail open to live fetch
        logger.debug("peer_cache: redis init failed: %s", exc)
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


async def close_peer_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        try:
            await _REDIS_CLIENT.close()
        except Exception:  # noqa: BLE001
            pass
        _REDIS_CLIENT = None


def _parse_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _get(redis_client: redis.Redis | None, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("peer_cache: GET failed for %s: %s", key, exc)
        return None
    return _parse_dict(raw)


async def _set(
    redis_client: redis.Redis | None, key: str, payload: dict[str, Any]
) -> None:
    if redis_client is None:
        return
    try:
        ttl = max(1, int(settings.naver_peer_cache_ttl_seconds))
        serialized = json.dumps(payload, default=str, ensure_ascii=False)
        await redis_client.set(key, serialized, ex=ttl)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("peer_cache: SET failed for %s: %s", key, exc)


async def get_cached_integration(
    redis_client: redis.Redis | None, code: str
) -> dict[str, Any] | None:
    return await _get(redis_client, f"{_INTEG_PREFIX}{code.upper()}")


async def set_cached_integration(
    redis_client: redis.Redis | None, code: str, payload: dict[str, Any]
) -> None:
    await _set(redis_client, f"{_INTEG_PREFIX}{code.upper()}", payload)


async def get_cached_sector(
    redis_client: redis.Redis | None, industry_code: str
) -> dict[str, Any] | None:
    return await _get(redis_client, f"{_SECTOR_PREFIX}{industry_code}")


async def set_cached_sector(
    redis_client: redis.Redis | None, industry_code: str, payload: dict[str, Any]
) -> None:
    await _set(redis_client, f"{_SECTOR_PREFIX}{industry_code}", payload)


__all__ = [
    "close_peer_cache_redis",
    "get_cached_integration",
    "get_cached_sector",
    "set_cached_integration",
    "set_cached_sector",
]
