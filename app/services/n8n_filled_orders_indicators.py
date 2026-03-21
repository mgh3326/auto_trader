"""Indicator enrichment for filled orders — computes N8nTradeReviewIndicators fields."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_adx,
    _calculate_ema,
    _calculate_macd,
    _calculate_rsi,
    _calculate_stoch_rsi,
    _fetch_ohlcv_for_indicators,
)
from app.services.external.fear_greed import fetch_fear_greed

logger = logging.getLogger(__name__)

_MIN_OHLCV_ROWS = 30


async def _compute_review_indicators(
    symbol: str,
    instrument_type: str,
) -> dict[str, Any] | None:
    """Compute indicators matching N8nTradeReviewIndicators fields.

    Returns dict with rsi_14, rsi_7, ema_20, ema_200, macd, macd_signal,
    adx, stoch_rsi_k, volume_ratio. Returns None on failure.
    """
    try:
        market_type = instrument_type  # crypto / equity_kr / equity_us
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

        if df.empty or len(df) < _MIN_OHLCV_ROWS:
            logger.warning("Insufficient OHLCV data for %s (%d rows)", symbol, len(df))
            return None

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        rsi_14_result = _calculate_rsi(close, period=14)
        rsi_7_result = _calculate_rsi(close, period=7)
        ema_result = _calculate_ema(close, periods=[20, 200])
        macd_result = _calculate_macd(close)
        adx_result = _calculate_adx(high, low, close)
        stoch_rsi_result = _calculate_stoch_rsi(close)

        # Volume ratio: last day volume / 20-day avg volume
        volume_ratio = _calc_volume_ratio(volume)

        return {
            "rsi_14": rsi_14_result.get("14"),
            "rsi_7": rsi_7_result.get("7"),
            "ema_20": ema_result.get("20"),
            "ema_200": ema_result.get("200"),
            "macd": macd_result.get("macd"),
            "macd_signal": macd_result.get("signal"),
            "adx": adx_result.get("adx"),
            "stoch_rsi_k": stoch_rsi_result.get("k"),
            "volume_ratio": volume_ratio,
        }
    except Exception as exc:
        logger.warning("Failed to compute review indicators for %s: %s", symbol, exc)
        return None


def _calc_volume_ratio(volume: pd.Series) -> float | None:
    """Calculate volume ratio: latest volume / 20-day average."""
    if len(volume) < 21:
        return None
    avg_20 = volume.iloc[-21:-1].mean()
    if pd.isna(avg_20) or avg_20 <= 0:
        return None
    latest = volume.iloc[-1]
    if pd.isna(latest):
        return None
    return round(float(latest / avg_20), 2)


_INDICATOR_CONCURRENCY = 5


async def _enrich_with_indicators(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich filled orders with technical indicators per unique symbol.

    - Deduplicates symbols (same symbol fetched once).
    - Fetches Fear & Greed Index once (crypto orders only).
    - Concurrency-limited to _INDICATOR_CONCURRENCY parallel fetches.
    - Best-effort: failures yield indicators=None for that order.
    """
    if not orders:
        return orders

    # Deduplicate: (symbol, instrument_type) -> indicators
    unique_symbols: dict[tuple[str, str], None] = {}
    for order in orders:
        key = (order["symbol"], order["instrument_type"])
        unique_symbols[key] = None

    sem = asyncio.Semaphore(_INDICATOR_CONCURRENCY)
    indicators_map: dict[str, dict[str, Any] | None] = {}

    async def _fetch_one(symbol: str, instrument_type: str) -> None:
        async with sem:
            result = await _compute_review_indicators(symbol, instrument_type)
            indicators_map[symbol] = result

    await asyncio.gather(
        *[_fetch_one(sym, itype) for sym, itype in unique_symbols],
        return_exceptions=True,
    )

    # Fetch Fear & Greed once if any crypto orders exist
    has_crypto = any(o["instrument_type"] == "crypto" for o in orders)
    fear_greed_value: int | None = None
    if has_crypto:
        try:
            fg_data = await fetch_fear_greed()
            if fg_data and isinstance(fg_data, dict):
                raw = fg_data.get("value")
                fear_greed_value = int(raw) if raw is not None else None
        except Exception as exc:
            logger.warning("Fear & Greed fetch failed: %s", exc)

    # Map indicators to orders
    for order in orders:
        base = indicators_map.get(order["symbol"])
        if base is None:
            order["indicators"] = None
            continue

        enriched = dict(base)
        if order["instrument_type"] == "crypto" and fear_greed_value is not None:
            enriched["fear_greed"] = fear_greed_value
        order["indicators"] = enriched

    return orders
