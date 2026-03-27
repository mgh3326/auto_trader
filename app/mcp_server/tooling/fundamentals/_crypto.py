"""Handlers for get_kimchi_premium and get_funding_rate tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate,
    _fetch_funding_rate_batch,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _normalize_crypto_base_symbol,
    _resolve_batch_crypto_symbols,
)
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_kimchi_premium
from app.mcp_server.tooling.shared import error_payload as _error_payload


async def handle_get_kimchi_premium(
    symbol: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        if symbol:
            sym = _normalize_crypto_base_symbol(symbol)
            if not sym:
                raise ValueError("symbol is required")
            symbols = [sym]
            return await _fetch_kimchi_premium(symbols)

        symbols = await _resolve_batch_crypto_symbols()
        payload = await _fetch_kimchi_premium(symbols)
        rows: list[dict[str, Any]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "symbol": item.get("symbol"),
                    "upbit_price": item.get("upbit_krw"),
                    "binance_price": item.get("binance_usdt"),
                    "premium_pct": item.get("premium_pct"),
                }
            )
        return rows
    except Exception as exc:
        return _error_payload(
            source="upbit+binance",
            message=str(exc),
            instrument_type="crypto",
        )


async def handle_get_funding_rate(
    symbol: str | None = None,
    limit: int = 10,
) -> dict[str, Any] | list[dict[str, Any]]:
    if symbol is not None and not symbol.strip():
        raise ValueError("symbol is required")

    try:
        if symbol is None:
            symbols = await _resolve_batch_crypto_symbols()
            return await _fetch_funding_rate_batch(symbols)

        normalized_symbol = _normalize_crypto_base_symbol(symbol)
        if not normalized_symbol:
            raise ValueError("symbol is required")

        capped_limit = min(max(limit, 1), 100)
        return await _fetch_funding_rate(normalized_symbol, capped_limit)
    except Exception as exc:
        normalized_symbol = _normalize_crypto_base_symbol(symbol or "")
        return _error_payload(
            source="binance",
            message=str(exc),
            symbol=f"{normalized_symbol}USDT" if normalized_symbol else None,
            instrument_type="crypto",
        )
