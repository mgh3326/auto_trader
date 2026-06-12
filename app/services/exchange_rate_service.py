from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict, cast

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_KEY = "usd_krw"
_OPEN_ER_API_CACHE_TTL_SECONDS = 300.0
_MIN_TOSS_CACHE_TTL_SECONDS = 1.0
_cache: dict[str, dict[str, object]] = {}
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


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _quote_cache_ttl_seconds(quote: UsdKrwExchangeRateQuote) -> float:
    if quote.source == "toss" and quote.valid_until is not None:
        ttl = (quote.valid_until - _now_utc()).total_seconds()
        return max(ttl, _MIN_TOSS_CACHE_TTL_SECONDS)
    return _OPEN_ER_API_CACHE_TTL_SECONDS


def _get_cached_quote(now: float) -> UsdKrwExchangeRateQuote | None:
    cached = _cache.get(_CACHE_KEY)
    if cached and float(cached["expires_at"]) > now:
        quote = cached.get("quote")
        if isinstance(quote, UsdKrwExchangeRateQuote):
            return quote
        rate = cached.get("rate")
        if rate is not None:
            scalar_rate = float(rate)
            return UsdKrwExchangeRateQuote(
                rate=scalar_rate,
                mid_rate=scalar_rate,
                source="open_er_api",
            )
    return None


def _set_cached_quote(quote: UsdKrwExchangeRateQuote, now: float) -> None:
    _cache[_CACHE_KEY] = {
        "quote": quote,
        "rate": quote.default_rate,
        "expires_at": now + _quote_cache_ttl_seconds(quote),
    }


async def _fetch_toss_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    from app.services.brokers.toss.client import TossReadClient

    client = TossReadClient.from_settings()
    try:
        raw = await client.exchange_rate(base_currency="USD", quote_currency="KRW")
    finally:
        await client.aclose()
    if not isinstance(raw, dict):
        raise TypeError("Toss exchange-rate response must be an object")
    quote = _parse_toss_usd_krw_quote(raw)
    logger.debug(
        "Fetched USD/KRW exchange rate from Toss: rate=%s mid_rate=%s valid_until=%s",
        quote.rate,
        quote.mid_rate,
        quote.valid_until,
    )
    return quote


async def _fetch_open_er_api_usd_krw_quote() -> UsdKrwExchangeRateQuote:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_EXCHANGE_RATE_URL)
        _ = response.raise_for_status()
        data = cast(_ExchangeRatePayload, response.json())

    quote = _parse_open_er_api_usd_krw_quote(data)
    logger.debug("Fetched USD/KRW exchange rate from open.er-api.com: %s", quote.rate)
    return quote


async def _fetch_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    if bool(getattr(settings, "toss_api_enabled", False)):
        try:
            return await _fetch_toss_usd_krw_quote()
        except Exception as exc:
            logger.warning(
                "Toss USD/KRW exchange-rate fetch failed; falling back to open.er-api.com: %s",
                exc,
            )
    return await _fetch_open_er_api_usd_krw_quote()


async def get_usd_krw_rate_details() -> UsdKrwExchangeRateQuote:
    now = time.monotonic()
    cached_quote = _get_cached_quote(now)
    if cached_quote is not None:
        return cached_quote

    async with _get_lock():
        now = time.monotonic()
        cached_quote = _get_cached_quote(now)
        if cached_quote is not None:
            return cached_quote

        quote = await _fetch_usd_krw_rate_details()
        _set_cached_quote(quote, now)
        return quote


async def get_usd_krw_rate() -> float:
    quote = await get_usd_krw_rate_details()
    return quote.default_rate


async def get_usd_krw_quote() -> float:
    """Return the default USD/KRW quote for existing scalar consumers."""
    return await get_usd_krw_rate()
