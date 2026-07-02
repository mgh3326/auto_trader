"""Redis ephemeral cache for ``analyze_stock_batch`` results (ROB-638).

Intraday repeat calls for the same symbol return a cached analysis result
instead of re-running the full pipeline. The cache stores the *full* analysis
dict (the output of ``_analyze_stock_impl``) so downstream formatting —
especially the per-batch holdings ``position`` lookup — always runs against
fresh holdings. Only the expensive market-data/indicator/opinion pipeline is
short-circuited on a cache hit.

TTL policy (per market):
    * KR     — refresh at the KRX session close (15:35 KST). Before 15:35 KST
               the entry is cached until 15:35 today; after 15:35 it is cached
               until the next midnight KST.
    * US     — refresh at the NYSE close (16:00 ET). Before 16:00 ET the entry
               is cached until 16:00 today; after 16:00 it is cached until the
               next midnight ET.
    * Crypto — flat 1 hour TTL (24/7 market, no session boundary).

Fail-open contract: every helper degrades gracefully. If Redis is unavailable
or returns malformed data, ``get_cached_analyze_result`` returns ``None`` and
``set_cached_analyze_result`` is a no-op — the caller always falls through to
the live analysis pipeline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.core.timezone import KST, now_kst
from app.services.ohlcv_cache_common import create_redis_client

logger = logging.getLogger(__name__)

# KRX regular-session close: cache refreshes at 15:35 KST (matches the OHLCV
# cache cutoff in app.services.kis_ohlcv_cache._KRX_DAILY_CACHE_CUTOFF).
_KR_SESSION_CLOSE = time(15, 35)
# NYSE regular-session close: 16:00 ET.
_US_SESSION_CLOSE = time(16, 0)
_US_EAST = ZoneInfo("America/New_York")
# Crypto trades 24/7 — short flat TTL.
_CRYPTO_TTL_SECONDS = 3600

# module-level singleton Redis client (mirrors kis_ohlcv_cache pattern).
_REDIS_CLIENT: redis.Redis | None = None


def _resolve_market_for_symbol(symbol: str, market: str | None) -> str:
    """Resolve a short market label ('kr' | 'us' | 'crypto') for cache keys.

    Uses the shared symbol/market resolver so the key matches the lane the
    analysis will actually run in. Falls back to the caller-supplied ``market``
    (lowercased) or 'unknown' if resolution raises — the key just needs to be
    deterministic and collision-free, not semantically perfect.
    """
    try:
        from app.mcp_server.tooling.shared import resolve_market_type

        market_type, _normalized = resolve_market_type(symbol, market)
        return {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}.get(
            market_type, market or "unknown"
        )
    except Exception:
        cleaned = (market or "").strip().lower()
        return cleaned or "unknown"


def _kst_date_for_key(now: datetime | None = None) -> str:
    """YYYY-MM-DD in KST — the date component of the cache key."""
    if now is None:
        kst = now_kst()
    elif now.tzinfo is None:
        kst = now.replace(tzinfo=KST)
    else:
        kst = now.astimezone(KST)
    return kst.date().isoformat()


def _cache_key(market: str, symbol: str, kst_date: str) -> str:
    """Build the Redis cache key for an analyze_batch result.

    Format: ``analyze_batch:{market}:{SYMBOL}:{YYYY-MM-DD}``
    """
    return f"analyze_batch:{market}:{symbol.upper()}:{kst_date}"


def _seconds_until(now: datetime, target_time: time, tz: ZoneInfo) -> int:
    """Seconds from ``now`` (aware) until the next occurrence of ``target_time``.

    If ``target_time`` is later today, returns the delta to today's wall-clock.
    Otherwise rolls to the same wall-clock tomorrow (delta < 24h).
    """
    now_local = now.astimezone(tz)
    target_today = datetime.combine(now_local.date(), target_time, tzinfo=tz)
    delta = (target_today - now_local).total_seconds()
    if delta < 0:
        # Already passed today — roll to tomorrow's wall-clock.
        delta += 24 * 3600
    return int(delta)


def _seconds_until_next_midnight(now: datetime, tz: ZoneInfo) -> int:
    """Seconds from ``now`` until the next 00:00 in ``tz``."""
    now_local = now.astimezone(tz)
    midnight_tomorrow = datetime.combine(
        now_local.date() + timedelta(days=1), time(0, 0), tzinfo=tz
    )
    return int((midnight_tomorrow - now_local).total_seconds())


def _cache_ttl_seconds(market: str, now: datetime) -> int:
    """Compute the per-market TTL (seconds) for a fresh cache entry.

    Args:
        market: 'kr' | 'us' | 'crypto' (case-insensitive).
        now: Aware UTC datetime used as the reference instant.
    """
    market = (market or "").strip().lower()
    if market == "crypto":
        return _CRYPTO_TTL_SECONDS

    if market == "kr":
        now_local = now.astimezone(KST)
        if now_local.time() < _KR_SESSION_CLOSE:
            # Trading window — cache until today's session close.
            return _seconds_until(now, _KR_SESSION_CLOSE, KST)
        # After close — cache until next midnight KST.
        return _seconds_until_next_midnight(now, KST)

    if market == "us":
        # NYSE/ET wall-clock reference.
        now_local = now.astimezone(_US_EAST)
        if now_local.time() < _US_SESSION_CLOSE:
            return _seconds_until(now, _US_SESSION_CLOSE, _US_EAST)
        return _seconds_until_next_midnight(now, _US_EAST)

    # Unknown market — conservative 15-minute TTL.
    return 900


async def _get_redis_client() -> redis.Redis | None:
    """Return the lazy module-level Redis client, or ``None`` if unavailable.

    The client is created lazily and cached. Any failure creating or configuring
    the client returns ``None`` so callers can fail-open to the live pipeline.
    """
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        _REDIS_CLIENT = await create_redis_client()
    except Exception as exc:
        logger.debug("analyze_cache: redis client init failed: %s", exc)
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


async def close_analyze_cache_redis() -> None:
    """Close the module-level Redis client (test/teardown helper)."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        try:
            await _REDIS_CLIENT.close()
        except Exception:
            pass
        _REDIS_CLIENT = None


def _normalize_cache_value(value: Any) -> dict[str, Any] | None:
    """Parse a cached Redis payload into a dict; ``None`` on malformed input."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


async def get_cached_analyze_result(
    redis_client: redis.Redis | None,
    market: str,
    symbol: str,
    kst_date: str,
) -> dict[str, Any] | None:
    """Return the cached full analysis dict for the symbol, or ``None``.

    ``None`` is returned on cache miss, malformed payload, or when Redis is
    unavailable (``redis_client is None``). Never raises.
    """
    if redis_client is None:
        return None
    key = _cache_key(market, symbol, kst_date)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.debug("analyze_cache: GET failed for %s: %s", key, exc)
        return None
    return _normalize_cache_value(raw)


async def set_cached_analyze_result(
    redis_client: redis.Redis | None,
    market: str,
    symbol: str,
    kst_date: str,
    result: dict[str, Any],
) -> None:
    """Cache ``result`` with the market-appropriate TTL. Never raises.

    Best-effort: if Redis is unavailable or serialization fails, the call is a
    no-op and the caller continues with the freshly-computed result.
    """
    if redis_client is None:
        return
    try:
        ttl = _cache_ttl_seconds(market, datetime.now(KST))
        if ttl <= 0:
            return
        payload = json.dumps(result, default=str, ensure_ascii=False)
        key = _cache_key(market, symbol, kst_date)
        await redis_client.set(key, payload, ex=ttl)
    except (TypeError, ValueError) as exc:
        logger.debug("analyze_cache: serialize failed for %s: %s", symbol, exc)
    except Exception as exc:
        logger.debug("analyze_cache: SET failed for %s: %s", symbol, exc)


__all__ = [
    "_cache_key",
    "_cache_ttl_seconds",
    "_kst_date_for_key",
    "_resolve_market_for_symbol",
    "close_analyze_cache_redis",
    "get_cached_analyze_result",
    "set_cached_analyze_result",
]
