"""Crypto composite score calculation utilities.

This module provides pure functions for calculating crypto-specific composite scores
based on RSI, volume ratio, candle patterns, and trend indicators (ADX/DI).

Score Formula:
    Total Score = (100 - RSI) * 0.4 + (Vol_Score * Candle_Coef) * 0.3 + Trend_Score * 0.3

Components:
- RSI Score: Lower RSI = higher score (oversold preference)
- Volume Score: Based on volume ratio vs 20-day average
- Candle Coefficient: Pattern-based coefficient from completed candle
- Trend Score: Based on ADX/DI direction and strength
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Candle type literals
CandleType = str
BULLISH: CandleType = "bullish"
HAMMER: CandleType = "hammer"
BEARISH_STRONG: CandleType = "bearish_strong"
BEARISH_NORMAL: CandleType = "bearish_normal"
FLAT: CandleType = "flat"


def calculate_candle_coefficient(
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
) -> tuple[float, CandleType]:
    """Calculate candle coefficient and type from OHLC values.

    Uses completed candle (index -2 if available, else -1).

    Rules (priority order):
    - total_range == 0 -> coef=0.5, type=flat
    - Bullish (close > open) -> coef=1.0, type=bullish
    - Lower shadow > body*2 -> coef=0.8, type=hammer
    - Body > range*0.7 and bearish -> coef=0.0, type=bearish_strong
    - Other bearish -> coef=0.5, type=bearish_normal

    Args:
        open_price: Candle open price
        high: Candle high price
        low: Candle low price
        close: Candle close price

    Returns:
        Tuple of (coefficient, candle_type)
    """
    if open_price is None or high is None or low is None or close is None:
        return 0.5, FLAT

    total_range = high - low
    if total_range <= 0:
        return 0.5, FLAT

    body = abs(close - open_price)
    lower_shadow = min(open_price, close) - low

    is_bullish = close > open_price

    if is_bullish:
        return 1.0, BULLISH

    # Check for hammer (lower shadow > body * 2)
    if lower_shadow > body * 2:
        return 0.8, HAMMER

    if body > total_range * 0.7:
        return 0.0, BEARISH_STRONG

    # Default bearish
    return 0.5, BEARISH_NORMAL


def calculate_volume_score(
    today_volume: float | None,
    avg_volume_20d: float | None,
) -> float:
    """Calculate volume score based on volume ratio.

    Formula: Vol_Score = min(vol_ratio * 33.3, 100)
    where vol_ratio = today_volume / avg_volume_20d

    Args:
        today_volume: Current day volume
        avg_volume_20d: 20-day average volume

    Returns:
        Volume score (0-100)
    """
    if today_volume is None or avg_volume_20d is None or avg_volume_20d <= 0:
        return 0.0

    vol_ratio = today_volume / avg_volume_20d
    vol_score = min(vol_ratio * 33.3, 100.0)
    return round(vol_score, 2)


def calculate_trend_score(
    adx: float | None,
    plus_di: float | None,
    minus_di: float | None,
) -> float:
    """Calculate trend score based on ADX and DI values.

    Rules:
    - plus_di > minus_di -> 90 (uptrend)
    - else adx < 35 -> 60 (weak trend)
    - else 35 <= adx <= 50 -> 30 (moderate trend)
    - else adx > 50 -> 10 (strong trend, possibly exhausted)

    Args:
        adx: Average Directional Index value
        plus_di: Plus Directional Indicator value
        minus_di: Minus Directional Indicator value

    Returns:
        Trend score
    """
    # If DI values available, check trend direction
    if plus_di is not None and minus_di is not None:
        if plus_di > minus_di:
            return 90.0

    # If ADX not available, return conservative default
    if adx is None:
        return 30.0

    # Determine score based on ADX strength
    if adx < 35:
        return 60.0
    elif adx <= 50:
        return 30.0
    else:
        return 10.0


def calculate_rsi_score(rsi: float | None) -> float:
    """Calculate RSI-based score component.

    Formula: RSI_Score = 100 - RSI
    This gives higher scores to lower (oversold) RSI values.

    Args:
        rsi: RSI value (0-100)

    Returns:
        RSI score (higher = more oversold)
    """
    if rsi is None:
        return 50.0  # Neutral default
    return 100.0 - rsi


def calculate_crypto_composite_score(
    rsi: float | None,
    volume_24h: float | None,
    avg_volume_20d: float | None,
    candle_coef: float,
    adx: float | None,
    plus_di: float | None,
    minus_di: float | None,
) -> float:
    """Calculate the final crypto composite score.

    Formula:
        Total Score = (100 - RSI) * 0.4 + (Vol_Score * Candle_Coef) * 0.3 + Trend_Score * 0.3

    Defaults for missing values:
    - RSI missing -> rsi_score = 50
    - ADX/DI missing -> trend_score = 30 (conservative)
    - Volume missing -> vol_score = 0

    Args:
        rsi: RSI value (0-100)
        volume_24h: Current 24h volume
        avg_volume_20d: 20-day average volume
        candle_coef: Candle pattern coefficient
        adx: ADX value
        plus_di: Plus DI value
        minus_di: Minus DI value

    Returns:
        Composite score clamped to 0-100
    """
    # RSI component (40%)
    rsi_score = calculate_rsi_score(rsi)
    rsi_component = rsi_score * 0.4

    # Volume component (30%)
    vol_score = calculate_volume_score(volume_24h, avg_volume_20d)
    vol_component = (vol_score * candle_coef) * 0.3

    # Trend component (30%)
    trend_score = calculate_trend_score(adx, plus_di, minus_di)
    trend_component = trend_score * 0.3

    total_score = rsi_component + vol_component + trend_component

    # Clamp to 0-100
    return round(max(0.0, min(100.0, total_score)), 2)


def calculate_20d_avg_volume(df: pd.DataFrame) -> float | None:
    """Calculate 20-day average volume from OHLCV DataFrame.

    Args:
        df: DataFrame with 'volume' column

    Returns:
        20-day average volume or None if insufficient data
    """
    if df is None or df.empty or "volume" not in df.columns:
        return None

    volumes = df["volume"].dropna()
    if len(volumes) < 1:
        return None

    # Use up to last 20 days
    recent_volumes = volumes.tail(20)
    return float(recent_volumes.mean())


def extract_candle_values(
    df: pd.DataFrame,
    candle_index: int = -2,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Extract OHLC values for a specific candle index.

    Args:
        df: OHLCV DataFrame with 'open', 'high', 'low', 'close' columns
        candle_index: Index of candle to extract (default: -2 for completed candle)

    Returns:
        Tuple of (open, high, low, close) values
    """
    if df is None or df.empty or len(df) < abs(candle_index):
        return None, None, None, None

    required_cols = ["open", "high", "low", "close"]
    for col in required_cols:
        if col not in df.columns:
            return None, None, None, None

    try:
        row = df.iloc[candle_index]
        return (
            float(row["open"]) if pd.notna(row["open"]) else None,
            float(row["high"]) if pd.notna(row["high"]) else None,
            float(row["low"]) if pd.notna(row["low"]) else None,
            float(row["close"]) if pd.notna(row["close"]) else None,
        )
    except (IndexError, KeyError, ValueError):
        return None, None, None, None


def calculate_crypto_metrics_from_ohlcv(
    df: pd.DataFrame,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "rsi": None,
        "volume_24h": None,
        "volume_ratio": None,
        "candle_coef": 0.5,
        "candle_type": FLAT,
        "adx": None,
        "plus_di": None,
        "minus_di": None,
        "score": None,
    }

    if df is None or df.empty:
        result["score"] = calculate_crypto_composite_score(
            rsi=None,
            volume_24h=None,
            avg_volume_20d=None,
            candle_coef=0.5,
            adx=None,
            plus_di=None,
            minus_di=None,
        )
        return result

    # Calculate RSI
    if "close" in df.columns and len(df) >= 14:
        from app.mcp_server.tooling.market_data_indicators import _calculate_rsi

        rsi_result = _calculate_rsi(df["close"])
        if rsi_result:
            result["rsi"] = rsi_result.get("14")

    # Get current volume
    if "volume" in df.columns and len(df) >= 1:
        try:
            result["volume_24h"] = float(df["volume"].iloc[-1])
        except (IndexError, ValueError, TypeError):
            pass

    # Calculate volume ratio
    avg_vol = calculate_20d_avg_volume(df)
    if result["volume_24h"] is not None and avg_vol is not None and avg_vol > 0:
        result["volume_ratio"] = round(result["volume_24h"] / avg_vol, 2)

    # Calculate candle coefficient (prefer completed candle at -2)
    candle_index = -2 if len(df) >= 2 else -1
    open_p, high, low, close = extract_candle_values(df, candle_index)
    candle_coef, candle_type = calculate_candle_coefficient(open_p, high, low, close)
    result["candle_coef"] = candle_coef
    result["candle_type"] = candle_type

    # Calculate ADX/DI
    if len(df) >= 14 and all(col in df.columns for col in ["high", "low", "close"]):
        try:
            adx_result = _calculate_adx_di(df)
            if adx_result:
                result["adx"] = adx_result.get("adx")
                result["plus_di"] = adx_result.get("plus_di")
                result["minus_di"] = adx_result.get("minus_di")
        except Exception as exc:
            logger.debug("ADX calculation failed: %s", exc)

    # Calculate final composite score
    result["score"] = calculate_crypto_composite_score(
        rsi=result["rsi"],
        volume_24h=result["volume_24h"],
        avg_volume_20d=avg_vol,
        candle_coef=result["candle_coef"],
        adx=result["adx"],
        plus_di=result["plus_di"],
        minus_di=result["minus_di"],
    )

    return result


def _calculate_adx_di(df: pd.DataFrame, period: int = 14) -> dict[str, float | None]:
    """Compatibility wrapper for shared ADX/DI calculation.

    This wrapper intentionally delegates to
    `market_data_indicators._calculate_adx` to avoid maintaining a second
    local ADX implementation in this module.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ADX calculation period (default 14)

    Returns:
        Dictionary with 'adx', 'plus_di', 'minus_di' values
    """
    if df is None or df.empty:
        return {"adx": None, "plus_di": None, "minus_di": None}

    try:
        from app.mcp_server.tooling.market_data_indicators import _calculate_adx

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        return _calculate_adx(high, low, close, period=period)
    except Exception as exc:
        logger.debug("ADX/DI calculation error: %s", exc)
        return {"adx": None, "plus_di": None, "minus_di": None}


__all__ = [
    "calculate_candle_coefficient",
    "calculate_volume_score",
    "calculate_trend_score",
    "calculate_rsi_score",
    "calculate_crypto_composite_score",
    "calculate_20d_avg_volume",
    "extract_candle_values",
    "calculate_crypto_metrics_from_ohlcv",
    "_calculate_adx_di",
    # Constants
    "BULLISH",
    "HAMMER",
    "BEARISH_STRONG",
    "BEARISH_NORMAL",
    "FLAT",
]
