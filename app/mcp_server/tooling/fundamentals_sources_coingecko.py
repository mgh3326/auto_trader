"""CoinGecko provider helpers for fundamentals domain."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_int as _to_optional_int,
)
from app.services import upbit as upbit_service

DEFAULT_BATCH_CRYPTO_SYMBOLS = [
    "BTC",
    "ETH",
    "XRP",
    "SOL",
    "ADA",
    "DOGE",
    "AVAX",
    "DOT",
    "TRX",
    "LINK",
]

COINGECKO_COINS_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_COINS_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_COIN_DETAIL_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
COINGECKO_CACHE_TTL_SECONDS = 300
COINGECKO_SYMBOL_ID_OVERRIDES = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "TRX": "tron",
    "LINK": "chainlink",
}

_COINGECKO_LIST_CACHE: dict[str, Any] = {"expires_at": 0.0, "symbol_to_ids": {}}
_COINGECKO_PROFILE_CACHE: dict[str, dict[str, Any]] = {}
_COINGECKO_LIST_LOCK = asyncio.Lock()
_COINGECKO_PROFILE_LOCK = asyncio.Lock()


def _normalize_crypto_base_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith(("KRW-", "USDT-", "BTC-")) and "-" in raw:
        return raw.split("-", 1)[1]
    if raw.endswith("USDT") and len(raw) > 4:
        return raw[: -len("USDT")]
    return raw


def _coingecko_cache_valid(expires_at: Any, now: float | None = None) -> bool:
    now_ts = time.time() if now is None else now
    try:
        return float(expires_at or 0) > now_ts
    except (TypeError, ValueError):
        return False


def _to_optional_money(value: Any) -> int | None:
    numeric = _to_optional_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _clean_description_one_line(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > 240:
        text = text[:240].rstrip() + "..."
    return text


def _map_coingecko_profile_to_output(profile: dict[str, Any]) -> dict[str, Any]:
    market_data = profile.get("market_data") or {}
    description_map = profile.get("description") or {}

    description = _clean_description_one_line(
        description_map.get("ko") or description_map.get("en")
    )
    market_cap_krw = _to_optional_money(
        (market_data.get("market_cap") or {}).get("krw")
    )
    total_volume_krw = _to_optional_money(
        (market_data.get("total_volume") or {}).get("krw")
    )
    ath_krw = _to_optional_money((market_data.get("ath") or {}).get("krw"))

    ath_change_pct = _to_optional_float(
        (market_data.get("ath_change_percentage") or {}).get("krw")
    )
    change_7d = _to_optional_float(
        (market_data.get("price_change_percentage_7d_in_currency") or {}).get("krw")
    )
    if change_7d is None:
        change_7d = _to_optional_float(market_data.get("price_change_percentage_7d"))

    change_30d = _to_optional_float(
        (market_data.get("price_change_percentage_30d_in_currency") or {}).get("krw")
    )
    if change_30d is None:
        change_30d = _to_optional_float(market_data.get("price_change_percentage_30d"))

    categories = profile.get("categories")
    if not isinstance(categories, list):
        categories = []

    return {
        "name": profile.get("name"),
        "symbol": str(profile.get("symbol") or "").upper() or None,
        "market_cap": market_cap_krw,
        "market_cap_rank": _to_optional_int(profile.get("market_cap_rank")),
        "total_volume_24h": total_volume_krw,
        "circulating_supply": _to_optional_float(market_data.get("circulating_supply")),
        "total_supply": _to_optional_float(market_data.get("total_supply")),
        "max_supply": _to_optional_float(market_data.get("max_supply")),
        "categories": categories,
        "description": description,
        "ath": ath_krw,
        "ath_change_percentage": ath_change_pct,
        "price_change_percentage_7d": change_7d,
        "price_change_percentage_30d": change_30d,
    }


async def _get_coingecko_symbol_to_ids() -> dict[str, list[str]]:
    now = time.time()
    if _coingecko_cache_valid(_COINGECKO_LIST_CACHE.get("expires_at"), now):
        cached = _COINGECKO_LIST_CACHE.get("symbol_to_ids")
        if isinstance(cached, dict):
            return cached

    async with _COINGECKO_LIST_LOCK:
        now = time.time()
        if _coingecko_cache_valid(_COINGECKO_LIST_CACHE.get("expires_at"), now):
            cached = _COINGECKO_LIST_CACHE.get("symbol_to_ids")
            if isinstance(cached, dict):
                return cached

        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COINS_LIST_URL,
                params={"include_platform": "false", "status": "active"},
            )
            response.raise_for_status()
            data = response.json()

        symbol_to_ids: dict[str, list[str]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                coin_id = str(item.get("id") or "").strip()
                coin_symbol = str(item.get("symbol") or "").strip().lower()
                if not coin_id or not coin_symbol:
                    continue
                symbol_to_ids.setdefault(coin_symbol, []).append(coin_id)

        _COINGECKO_LIST_CACHE["symbol_to_ids"] = symbol_to_ids
        _COINGECKO_LIST_CACHE["expires_at"] = now + COINGECKO_CACHE_TTL_SECONDS
        return symbol_to_ids


async def _choose_coingecko_id_by_market_cap(candidate_ids: list[str]) -> str | None:
    if not candidate_ids:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COINS_MARKETS_URL,
                params={
                    "vs_currency": "krw",
                    "ids": ",".join(candidate_ids),
                    "order": "market_cap_desc",
                    "per_page": len(candidate_ids),
                    "page": 1,
                    "sparkline": "false",
                },
            )
            response.raise_for_status()
            markets = response.json()

        if isinstance(markets, list) and markets:
            first = markets[0]
            if isinstance(first, dict):
                top_id = str(first.get("id") or "").strip()
                if top_id:
                    return top_id
    except Exception:
        return None

    return None


async def _resolve_coingecko_coin_id(symbol: str) -> str:
    base_symbol = _normalize_crypto_base_symbol(symbol)
    if not base_symbol:
        raise ValueError("symbol is required")

    override = COINGECKO_SYMBOL_ID_OVERRIDES.get(base_symbol)
    if override:
        return override

    symbol_to_ids = await _get_coingecko_symbol_to_ids()
    candidates = symbol_to_ids.get(base_symbol.lower(), [])
    if not candidates:
        raise ValueError(f"CoinGecko id not found for symbol '{base_symbol}'")

    if len(candidates) == 1:
        return candidates[0]

    base_lower = base_symbol.lower()
    for coin_id in candidates:
        if coin_id == base_lower or coin_id.replace("-", "") == base_lower:
            return coin_id

    top_id = await _choose_coingecko_id_by_market_cap(candidates)
    if top_id:
        return top_id

    return sorted(candidates)[0]


async def _fetch_coingecko_coin_profile(coin_id: str) -> dict[str, Any]:
    cache_key = coin_id.strip().lower()
    if not cache_key:
        raise ValueError("coin_id is required")

    now = time.time()
    cached = _COINGECKO_PROFILE_CACHE.get(cache_key)
    if cached and _coingecko_cache_valid(cached.get("expires_at"), now):
        data = cached.get("data")
        if isinstance(data, dict):
            return data

    async with _COINGECKO_PROFILE_LOCK:
        now = time.time()
        cached = _COINGECKO_PROFILE_CACHE.get(cache_key)
        if cached and _coingecko_cache_valid(cached.get("expires_at"), now):
            data = cached.get("data")
            if isinstance(data, dict):
                return data

        async with httpx.AsyncClient(timeout=15) as cli:
            response = await cli.get(
                COINGECKO_COIN_DETAIL_URL.format(coin_id=cache_key),
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "true",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                    "include_categories_details": "false",
                },
            )
            response.raise_for_status()
            data = response.json()

        _COINGECKO_PROFILE_CACHE[cache_key] = {
            "expires_at": now + COINGECKO_CACHE_TTL_SECONDS,
            "data": data,
        }
        return data


async def _resolve_batch_crypto_symbols() -> list[str]:
    try:
        coins = await upbit_service.fetch_my_coins()
        held_symbols: list[str] = []
        for coin in coins:
            currency = str(coin.get("currency", "")).upper().strip()
            if not currency or currency == "KRW":
                continue
            quantity = _to_float(coin.get("balance")) + _to_float(coin.get("locked"))
            if quantity <= 0:
                continue
            held_symbols.append(currency)

        if held_symbols:
            try:
                tradable_markets = await upbit_service.fetch_all_market_codes(fiat=None)
                tradable_set = {str(market).upper() for market in tradable_markets}
                held_symbols = [
                    symbol for symbol in held_symbols if symbol.upper() in tradable_set
                ]
            except Exception:
                pass

            if held_symbols:
                return sorted(set(held_symbols))
    except Exception:
        pass

    return list(DEFAULT_BATCH_CRYPTO_SYMBOLS)


__all__ = [
    "_COINGECKO_LIST_CACHE",
    "_COINGECKO_PROFILE_CACHE",
    "_clean_description_one_line",
    "_fetch_coingecko_coin_profile",
    "_map_coingecko_profile_to_output",
    "_normalize_crypto_base_symbol",
    "_resolve_batch_crypto_symbols",
    "_resolve_coingecko_coin_id",
    "_to_optional_money",
]
