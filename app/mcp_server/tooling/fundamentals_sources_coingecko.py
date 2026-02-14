"""CoinGecko provider helpers for fundamentals domain."""

from __future__ import annotations

from typing import Any

import app.mcp_server.tooling.fundamentals_sources_naver as _naver_sources

_COINGECKO_LIST_CACHE = _naver_sources._COINGECKO_LIST_CACHE
_COINGECKO_PROFILE_CACHE = _naver_sources._COINGECKO_PROFILE_CACHE


def _normalize_crypto_base_symbol(symbol: str) -> str:
    return _naver_sources._normalize_crypto_base_symbol(symbol)


async def _resolve_coingecko_coin_id(symbol: str) -> str:
    return await _naver_sources._resolve_coingecko_coin_id(symbol)


async def _fetch_coingecko_coin_profile(coin_id: str) -> dict[str, Any]:
    return await _naver_sources._fetch_coingecko_coin_profile(coin_id)


async def _resolve_batch_crypto_symbols() -> list[str]:
    return await _naver_sources._resolve_batch_crypto_symbols()


__all__ = [
    "_COINGECKO_LIST_CACHE",
    "_COINGECKO_PROFILE_CACHE",
    "_fetch_coingecko_coin_profile",
    "_normalize_crypto_base_symbol",
    "_resolve_batch_crypto_symbols",
    "_resolve_coingecko_coin_id",
]
