"""Market context service — collects macro and symbol-specific context."""

from __future__ import annotations

import logging
from typing import Any

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_adx,
    _calculate_ema,
    _calculate_rsi,
    _calculate_stoch_rsi,
    _fetch_ohlcv_for_indicators,
)

logger = logging.getLogger(__name__)


def _normalize_crypto_symbol(symbol: str) -> str:
    """Normalize crypto symbol to KRW-XXX format."""
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return ""
    if "-" not in normalized:
        return f"KRW-{normalized}"
    return normalized


def _classify_trend(rsi_14: float | None, ema_distance_pct: float | None) -> str:
    """Classify trend based on RSI and EMA distance."""
    if rsi_14 is None:
        return "neutral"

    if rsi_14 > 55 and (ema_distance_pct is not None and ema_distance_pct > 0):
        return "bullish"
    elif rsi_14 < 45 and (ema_distance_pct is not None and ema_distance_pct < 0):
        return "bearish"
    else:
        return "neutral"


def _classify_strength(adx: float | None) -> str:
    """Classify trend strength based on ADX."""
    if adx is None:
        return "weak"
    if adx > 40:
        return "strong"
    elif adx > 25:
        return "moderate"
    else:
        return "weak"


async def _compute_symbol_indicators(symbol: str) -> dict[str, Any] | None:
    """
    Compute all technical indicators for a single symbol.

    Returns dict with indicator values or None on failure.
    """
    raw_symbol = _normalize_crypto_symbol(symbol)

    try:
        df = await _fetch_ohlcv_for_indicators(raw_symbol, "crypto", count=200)

        if df.empty or len(df) < 30:
            logger.warning(f"Insufficient OHLCV data for {symbol}")
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]

        rsi_14_result = _calculate_rsi(close, period=14)
        rsi_7_result = _calculate_rsi(close, period=7)
        stoch_rsi_result = _calculate_stoch_rsi(close)
        adx_result = _calculate_adx(high, low, close)
        ema_result = _calculate_ema(close, periods=[20])

        rsi_14 = rsi_14_result.get("14")
        rsi_7 = rsi_7_result.get("7")
        stoch_rsi_k = stoch_rsi_result.get("k")
        stoch_rsi_d = stoch_rsi_result.get("d")
        adx = adx_result.get("adx")
        ema_20 = ema_result.get("20")

        current_price = float(close.iloc[-1])
        ema_20_distance_pct = None
        if ema_20 and ema_20 > 0:
            ema_20_distance_pct = round((current_price - ema_20) / ema_20 * 100, 2)

        trend = _classify_trend(rsi_14, ema_20_distance_pct)
        trend_strength = _classify_strength(adx)

        return {
            "rsi_14": rsi_14,
            "rsi_7": rsi_7,
            "stoch_rsi_k": stoch_rsi_k,
            "stoch_rsi_d": stoch_rsi_d,
            "adx": adx,
            "ema_20_distance_pct": ema_20_distance_pct,
            "trend": trend,
            "trend_strength": trend_strength,
            "current_price": current_price,
        }

    except Exception as exc:
        logger.warning(f"Failed to compute indicators for {symbol}: {exc}")
        return None
