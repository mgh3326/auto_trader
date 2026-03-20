from __future__ import annotations

import asyncio
import logging
import time
from typing import TypedDict, cast

import httpx

logger = logging.getLogger(__name__)

_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_KEY = "usd_krw"
_CACHE_TTL_SECONDS = 300.0
_cache: dict[str, dict[str, float]] = {}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


class _ExchangeRatePayload(TypedDict):
    rates: dict[str, float]


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _get_cached_rate(now: float) -> float | None:
    cached = _cache.get(_CACHE_KEY)
    if cached and cached["expires_at"] > now:
        return cached["rate"]
    return None


async def _fetch_usd_krw_rate() -> float:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_EXCHANGE_RATE_URL)
        _ = response.raise_for_status()
        data = cast(_ExchangeRatePayload, response.json())

    rate = float(data["rates"]["KRW"])
    logger.debug("Fetched USD/KRW exchange rate: %s", rate)
    return rate


async def get_usd_krw_rate() -> float:
    now = time.monotonic()
    cached_rate = _get_cached_rate(now)
    if cached_rate is not None:
        return cached_rate

    async with _get_lock():
        now = time.monotonic()
        cached_rate = _get_cached_rate(now)
        if cached_rate is not None:
            return cached_rate

        rate = await _fetch_usd_krw_rate()
        _cache[_CACHE_KEY] = {
            "rate": rate,
            "expires_at": now + _CACHE_TTL_SECONDS,
        }
        return rate
