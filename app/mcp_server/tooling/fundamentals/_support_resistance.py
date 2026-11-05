"""Handler for get_support_resistance tool."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _calculate_volume_profile,
    _cluster_price_levels,
    _collect_support_resistance_price_levels,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
    _split_support_resistance_levels,
)
from app.mcp_server.tooling.market_data_quotes import fetch_us_live_last_price
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)


async def get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Get support/resistance zones from multi-indicator clustering."""

    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    try:
        if preloaded_df is not None and not preloaded_df.empty:
            df = preloaded_df
        else:
            df = await _fetch_ohlcv_for_indicators(
                normalized_symbol, market_type, count=60
            )
        if df.empty:
            raise ValueError(f"No data available for symbol '{normalized_symbol}'")

        for col in ("high", "low", "close"):
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        current_price = round(float(df["close"].iloc[-1]), 2)
        current_price_source = "ohlcv_close"
        if market_type == "equity_us":
            # ROB-365 bug 1: position levels against the live intraday price,
            # not the previous daily close (levels themselves stay historical).
            live = await fetch_us_live_last_price(normalized_symbol)
            if live is not None:
                current_price = round(live, 2)
                current_price_source = "yahoo_live"
        fib_result = _calculate_fibonacci(df, current_price)
        fib_result["symbol"] = normalized_symbol

        volume_result = _calculate_volume_profile(df, bins=20)
        volume_result["symbol"] = normalized_symbol
        volume_result["period_days"] = 60

        indicator_result = _compute_indicators(df, ["bollinger"])

        if not fib_result.get("levels"):
            raise ValueError("Failed to calculate Fibonacci levels")
        if current_price <= 0:
            raise ValueError("failed to resolve current price")

        price_levels = _collect_support_resistance_price_levels(
            fib_result, volume_result, indicator_result
        )

        clustered_levels = _cluster_price_levels(price_levels, tolerance_pct=0.02)
        supports, resistances = _split_support_resistance_levels(
            clustered_levels,
            current_price,
        )

        result: dict[str, Any] = {
            "symbol": normalized_symbol,
            "current_price": round(current_price, 2),
            "supports": supports,
            "resistances": resistances,
        }
        if market_type == "equity_us":
            result["current_price_source"] = current_price_source
            result["current_price_stale"] = current_price_source != "yahoo_live"
        return result
    except Exception as exc:
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type=market_type,
        )


_DEFAULT_GET_SUPPORT_RESISTANCE_IMPL = get_support_resistance_impl
