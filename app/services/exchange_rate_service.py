from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import logging
import time
from typing import Any, Literal, TypedDict, cast

import httpx

logger = logging.getLogger(__name__)

_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_KEY = "usd_krw"
_CACHE_TTL_SECONDS = 300.0
_cache: dict[str, dict[str, float]] = {}
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True)
class UsdKrwExchangeRateQuote:
    rate: float
    mid_rate: float
    source: Literal["toss", "open_er_api"]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    basis_point: float | None = None
    rate_change_type: str | None = None

    @property
    def default_rate(self) -> float:
        return self.mid_rate


class _ExchangeRatePayload(TypedDict):
    rates: dict[str, float]


def _parse_decimal_float(value: object) -> float:
    if isinstance(value, float):
        raise TypeError("Toss decimal values must be strings, not float")
    if value is None:
        raise TypeError("Decimal value is required")
    return float(Decimal(str(value)))


def _parse_optional_decimal_float(value: object) -> float | None:
    if value is None:
        return None
    return _parse_decimal_float(value)


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_toss_usd_krw_quote(raw: dict[str, Any]) -> UsdKrwExchangeRateQuote:
    if raw.get("baseCurrency") != "USD" or raw.get("quoteCurrency") != "KRW":
        raise ValueError("Toss exchange-rate response is not USD/KRW")
    return UsdKrwExchangeRateQuote(
        rate=_parse_decimal_float(raw["rate"]),
        mid_rate=_parse_decimal_float(raw["midRate"]),
        source="toss",
        valid_from=_parse_datetime(raw.get("validFrom")),
        valid_until=_parse_datetime(raw.get("validUntil")),
        basis_point=_parse_optional_decimal_float(raw.get("basisPoint")),
        rate_change_type=str(raw["rateChangeType"])
        if raw.get("rateChangeType") is not None
        else None,
    )


def _parse_open_er_api_usd_krw_quote(
    data: _ExchangeRatePayload,
) -> UsdKrwExchangeRateQuote:
    rate = float(data["rates"]["KRW"])
    return UsdKrwExchangeRateQuote(
        rate=rate,
        mid_rate=rate,
        source="open_er_api",
    )


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


async def get_usd_krw_quote() -> float:
    """Return the current USD/KRW quote for watch-alert FX checks."""
    return await get_usd_krw_rate()
