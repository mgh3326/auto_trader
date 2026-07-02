"""Redis ephemeral cache for slowly-changing analyze FETCH outputs (ROB-638).

Fetch-layer cache — NOT a whole-response cache. Only slowly-changing provider
fetch outputs are cached:

    * ``naver``            — KR Naver analysis snapshot (valuation/news/opinions)
    * ``yfinance``         — US yfinance valuation + investment-opinions bundle
    * ``finnhub_profile``  — US Finnhub company profile

Live surfaces (quote/price, position lookup, RSI/indicators, support/resistance
computation and the intraday re-sign, recommendation) are recomputed on EVERY
call. Crypto is never cached (no analyst-consensus source).

Key format: ``analyze_fetch:{provider}:{SYMBOL}:{date}`` where ``symbol`` is the
resolver-normalized symbol and ``date`` is provider-local:

    * ``naver``                        — date in KST
    * ``yfinance`` / ``finnhub_profile`` — date in ET (America/New_York), so the
      key date and the TTL reference share the same timezone (no orphan keys at
      00:00 KST).

TTL policy (per provider):
    * ``naver``     — refresh at the KRX session close (15:35 KST). Before close
                      the entry lives until 15:35 today; after close until the
                      next midnight KST.
    * ``yfinance`` / ``finnhub_profile`` — refresh at the NYSE close (16:00 ET).
                      Before close the entry lives until 16:00 ET today; after
                      close until the next midnight ET.

Stored envelope: ``{"fetched_at": <ISO KST>, "payload": {...}}`` — callers use
``fetched_at`` as the per-symbol ``derived_as_of`` label.

Hermetic guard: ``settings.analyze_fetch_cache_enabled`` (default ``True``;
forced ``false`` in ``tests/conftest.py``). When disabled the client factory
returns ``None`` and every caller fails open to a direct fetch — no test can
touch a real Redis unless it explicitly patches ``_get_redis_client``.

Fail-open contract: every helper degrades gracefully. If Redis is unavailable
or returns malformed data, ``get_cached_fetch_payload`` returns ``(None, None)``
and ``set_cached_fetch_payload`` is a no-op — the caller always falls through
to the live provider fetch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.core.config import settings
from app.core.timezone import KST, now_kst
from app.services.ohlcv_cache_common import create_redis_client

logger = logging.getLogger(__name__)

PROVIDER_NAVER = "naver"
PROVIDER_YFINANCE = "yfinance"
PROVIDER_FINNHUB_PROFILE = "finnhub_profile"

# KRX regular-session close: cache refreshes at 15:35 KST (matches the OHLCV
# cache cutoff in app.services.kis_ohlcv_cache._KRX_DAILY_CACHE_CUTOFF).
_KR_SESSION_CLOSE = time(15, 35)
# NYSE regular-session close: 16:00 ET.
_US_SESSION_CLOSE = time(16, 0)
_US_EAST = ZoneInfo("America/New_York")

# Provider-local timezone for the key date AND the TTL reference clock.
_PROVIDER_TZS: dict[str, ZoneInfo] = {
    PROVIDER_NAVER: KST,
    PROVIDER_YFINANCE: _US_EAST,
    PROVIDER_FINNHUB_PROFILE: _US_EAST,
}

# Unknown provider — conservative 15-minute TTL.
_UNKNOWN_PROVIDER_TTL_SECONDS = 900

# module-level singleton Redis client (mirrors kis_ohlcv_cache pattern).
_REDIS_CLIENT: redis.Redis | None = None


def _provider_tz(provider: str) -> ZoneInfo:
    return _PROVIDER_TZS.get((provider or "").strip().lower(), KST)


def _provider_date_for_key(provider: str, now: datetime | None = None) -> str:
    """YYYY-MM-DD in the provider-local timezone (KST for naver, ET for US)."""
    if now is None:
        now = now_kst()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    return now.astimezone(_provider_tz(provider)).date().isoformat()


def _fetch_cache_key(provider: str, symbol: str, now: datetime | None = None) -> str:
    """Build the Redis cache key for a provider fetch payload.

    Format: ``analyze_fetch:{provider}:{SYMBOL}:{YYYY-MM-DD}`` — the date is
    provider-local so US keys roll at ET midnight (matching their TTL clock).
    ``symbol`` must be the resolver-normalized symbol.
    """
    date_part = _provider_date_for_key(provider, now)
    return f"analyze_fetch:{provider}:{symbol.upper()}:{date_part}"


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


def _fetch_cache_ttl_seconds(provider: str, now: datetime) -> int:
    """Compute the per-provider TTL (seconds) for a fresh cache entry.

    Args:
        provider: 'naver' | 'yfinance' | 'finnhub_profile' (case-insensitive).
        now: Aware datetime used as the reference instant.
    """
    provider = (provider or "").strip().lower()
    if provider == PROVIDER_NAVER:
        now_local = now.astimezone(KST)
        if now_local.time() < _KR_SESSION_CLOSE:
            # Trading window — cache until today's session close (15:35 KST).
            return _seconds_until(now, _KR_SESSION_CLOSE, KST)
        # After close — cache until next midnight KST.
        return _seconds_until_next_midnight(now, KST)

    if provider in (PROVIDER_YFINANCE, PROVIDER_FINNHUB_PROFILE):
        # NYSE/ET wall-clock reference — same tz as the key date.
        now_local = now.astimezone(_US_EAST)
        if now_local.time() < _US_SESSION_CLOSE:
            return _seconds_until(now, _US_SESSION_CLOSE, _US_EAST)
        return _seconds_until_next_midnight(now, _US_EAST)

    return _UNKNOWN_PROVIDER_TTL_SECONDS


async def _get_redis_client() -> redis.Redis | None:
    """Return the lazy module-level Redis client, or ``None`` if unavailable.

    Returns ``None`` (cache disabled, fail-open to direct fetch) when
    ``settings.analyze_fetch_cache_enabled`` is False — the hermetic guard for
    tests. Otherwise the client is created lazily and cached; any failure
    creating it returns ``None`` so callers fail open to the live fetch.
    """
    global _REDIS_CLIENT
    if not settings.analyze_fetch_cache_enabled:
        return None
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


def _normalize_cache_envelope(value: Any) -> dict[str, Any] | None:
    """Parse a cached Redis payload into an envelope dict; ``None`` if malformed.

    A valid envelope is ``{"fetched_at": str, "payload": dict}``.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("payload"), dict):
        return None
    if not isinstance(parsed.get("fetched_at"), str):
        return None
    return parsed


async def get_cached_fetch_payload(
    redis_client: redis.Redis | None,
    provider: str,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(payload, fetched_at)`` for the cached provider fetch, or
    ``(None, None)``.

    ``(None, None)`` is returned on cache miss, malformed payload, or when
    Redis is unavailable (``redis_client is None``). Never raises.
    """
    if redis_client is None:
        return None, None
    key = _fetch_cache_key(provider, symbol)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        logger.debug("analyze_cache: GET failed for %s: %s", key, exc)
        return None, None
    envelope = _normalize_cache_envelope(raw)
    if envelope is None:
        return None, None
    return envelope["payload"], envelope["fetched_at"]


async def set_cached_fetch_payload(
    redis_client: redis.Redis | None,
    provider: str,
    symbol: str,
    payload: dict[str, Any],
    *,
    fetched_at: str | None = None,
) -> None:
    """Cache a provider fetch ``payload`` with the provider TTL. Never raises.

    Best-effort: if Redis is unavailable or serialization fails, the call is a
    no-op and the caller continues with the freshly-fetched payload. Callers
    must NOT invoke this for degraded/empty provider results.
    """
    if redis_client is None:
        return
    try:
        now = now_kst()
        ttl = _fetch_cache_ttl_seconds(provider, now)
        if ttl <= 0:
            return
        envelope = {
            "fetched_at": fetched_at or now.isoformat(),
            "payload": payload,
        }
        serialized = json.dumps(envelope, default=str, ensure_ascii=False)
        key = _fetch_cache_key(provider, symbol, now)
        await redis_client.set(key, serialized, ex=ttl)
    except (TypeError, ValueError) as exc:
        logger.debug("analyze_cache: serialize failed for %s: %s", symbol, exc)
    except Exception as exc:
        logger.debug("analyze_cache: SET failed for %s: %s", symbol, exc)


__all__ = [
    "PROVIDER_FINNHUB_PROFILE",
    "PROVIDER_NAVER",
    "PROVIDER_YFINANCE",
    "_fetch_cache_key",
    "_fetch_cache_ttl_seconds",
    "_provider_date_for_key",
    "close_analyze_cache_redis",
    "get_cached_fetch_payload",
    "set_cached_fetch_payload",
]
