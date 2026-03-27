"""Handler for get_support_resistance tool."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _calculate_volume_profile,
    _cluster_price_levels,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
    _format_fibonacci_source,
    _split_support_resistance_levels,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
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

        price_levels: list[tuple[float, str]] = []

        fib_levels = fib_result.get("levels", {})
        if isinstance(fib_levels, dict):
            for level_key, price in fib_levels.items():
                level_price = _to_optional_float(price)
                if level_price is None or level_price <= 0:
                    continue
                price_levels.append(
                    (level_price, _format_fibonacci_source(str(level_key)))
                )

        poc_price = _to_optional_float((volume_result.get("poc") or {}).get("price"))
        if poc_price is not None and poc_price > 0:
            price_levels.append((poc_price, "volume_poc"))

        value_area = volume_result.get("value_area") or {}
        value_area_high = _to_optional_float(value_area.get("high"))
        value_area_low = _to_optional_float(value_area.get("low"))
        if value_area_high is not None and value_area_high > 0:
            price_levels.append((value_area_high, "volume_value_area_high"))
        if value_area_low is not None and value_area_low > 0:
            price_levels.append((value_area_low, "volume_value_area_low"))

        bollinger_raw = indicator_result.get("bollinger")
        if isinstance(bollinger_raw, dict):
            bollinger = bollinger_raw
        else:
            indicators_raw = indicator_result.get("indicators")
            if isinstance(indicators_raw, dict):
                nested_bollinger = indicators_raw.get("bollinger")
                bollinger = (
                    nested_bollinger if isinstance(nested_bollinger, dict) else {}
                )
            else:
                bollinger = {}
        bb_upper = _to_optional_float(bollinger.get("upper"))
        bb_middle = _to_optional_float(bollinger.get("middle"))
        bb_lower = _to_optional_float(bollinger.get("lower"))
        if bb_upper is not None and bb_upper > 0:
            price_levels.append((bb_upper, "bb_upper"))
        if bb_middle is not None and bb_middle > 0:
            price_levels.append((bb_middle, "bb_middle"))
        if bb_lower is not None and bb_lower > 0:
            price_levels.append((bb_lower, "bb_lower"))

        clustered_levels = _cluster_price_levels(price_levels, tolerance_pct=0.02)
        supports, resistances = _split_support_resistance_levels(
            clustered_levels,
            current_price,
        )

        return {
            "symbol": normalized_symbol,
            "current_price": round(current_price, 2),
            "supports": supports,
            "resistances": resistances,
        }
    except Exception as exc:
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type=market_type,
        )


_DEFAULT_GET_SUPPORT_RESISTANCE_IMPL = get_support_resistance_impl
