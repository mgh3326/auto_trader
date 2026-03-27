"""Handlers for get_company_profile and get_crypto_profile tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import (
    normalize_equity_market,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _fetch_coingecko_coin_profile,
    _map_coingecko_profile_to_output,
    _normalize_crypto_base_symbol,
    _resolve_coingecko_coin_id,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_company_profile_naver,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)


async def handle_get_company_profile(
    symbol: str,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Company profile is not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_company_profile_naver(symbol)
        return await _fetch_company_profile_finnhub(symbol)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "finnhub"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_crypto_profile(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    normalized_symbol = _normalize_crypto_base_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("symbol is required")

    try:
        coin_id = await _resolve_coingecko_coin_id(normalized_symbol)
        profile = await _fetch_coingecko_coin_profile(coin_id)
        result = _map_coingecko_profile_to_output(profile)
        if result.get("symbol") is None:
            result["symbol"] = normalized_symbol
        if result.get("name") is None:
            result["name"] = normalized_symbol
        return result
    except Exception as exc:
        return _error_payload(
            source="coingecko",
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type="crypto",
        )
