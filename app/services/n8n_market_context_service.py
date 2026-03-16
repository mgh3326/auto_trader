"""Market context service for n8n integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import pandas as pd

from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_adx,
    _calculate_ema,
    _calculate_rsi,
    _calculate_stoch_rsi,
    _fetch_ohlcv_for_indicators,
)
from app.schemas.n8n import (
    N8nEconomicEvent,
    N8nFearGreedData,
    N8nMarketContextSummary,
    N8nMarketOverview,
    N8nSymbolContext,
)
from app.services.brokers.upbit.client import fetch_multiple_tickers
from app.services.external.economic_calendar import fetch_economic_events_today
from app.services.external.fear_greed import fetch_fear_greed
from app.services.n8n_formatting import fmt_gap, fmt_price
from app.services.n8n_pending_orders_service import fetch_pending_orders

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


def _fmt_volume_krw(volume_krw: float | None) -> str:
    """Format volume in Korean units (억)."""
    if volume_krw is None:
        return "-"
    if volume_krw == 0:
        return "0"
    if volume_krw >= 100_000_000:
        return f"{volume_krw / 100_000_000:,.0f}억"
    if volume_krw >= 10_000:
        return f"{volume_krw / 10_000:,.1f}만"
    return f"{volume_krw:,.0f}"


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
            "adx": adx,
            "ema_20_distance_pct": ema_20_distance_pct,
            "trend": trend,
            "trend_strength": trend_strength,
            "current_price": current_price,
        }

    except Exception as exc:
        logger.warning(f"Failed to compute indicators for {symbol}: {exc}")
        return None


async def _fetch_crypto_market_overview() -> dict[str, Any]:
    """Fetch crypto market overview data (BTC dominance, market cap change)."""
    try:
        tickers = await fetch_multiple_tickers(["KRW-BTC", "KRW-ETH"])

        btc_dominance = None
        total_market_cap_change = None

        return {
            "btc_dominance": btc_dominance,
            "total_market_cap_change_24h": total_market_cap_change,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch market overview: {exc}")
        return {
            "btc_dominance": None,
            "total_market_cap_change_24h": None,
        }


async def fetch_market_context(
    market: str = "crypto",
    symbols: list[str] | None = None,
    include_fear_greed: bool = True,
    include_economic_calendar: bool = True,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """
    Fetch comprehensive market context for given symbols.

    Args:
        market: Market type (crypto, kr, us, all)
        symbols: List of symbols to analyze (if None, fetches from pending orders + holdings)
        include_fear_greed: Whether to include Fear & Greed Index
        include_economic_calendar: Whether to include economic events
        as_of: Timestamp for the response

    Returns:
        Dict with market_overview, symbols list, summary, and errors
    """
    if as_of is None:
        as_of = now_kst()

    errors: list[dict[str, object]] = []

    if symbols is None:
        try:
            pending_result = await fetch_pending_orders(
                market=market,
                min_amount=0,
                include_current_price=False,
                side=None,
                as_of=as_of,
            )
            pending_symbols = [
                order["symbol"]
                for order in pending_result.get("orders", [])
                if order.get("market") == "crypto"
            ]
        except Exception as exc:
            logger.warning(f"Failed to fetch pending orders: {exc}")
            pending_symbols = []
            errors.append({"source": "pending_orders", "error": str(exc)})

        symbols = list(set(pending_symbols)) if pending_symbols else ["BTC"]

    if market != "crypto":
        errors.append(
            {
                "source": "market",
                "error": f"Market '{market}' not yet supported, using crypto",
            }
        )
        market = "crypto"

    fear_greed_task = fetch_fear_greed() if include_fear_greed else asyncio.sleep(0)
    econ_calendar_task = (
        fetch_economic_events_today() if include_economic_calendar else asyncio.sleep(0)
    )
    market_overview_task = _fetch_crypto_market_overview()

    fear_greed_data, econ_events, market_overview = await asyncio.gather(
        fear_greed_task,
        econ_calendar_task,
        market_overview_task,
        return_exceptions=True,
    )

    if isinstance(fear_greed_data, Exception):
        errors.append({"source": "fear_greed", "error": str(fear_greed_data)})
        fear_greed_data = None

    if isinstance(econ_events, Exception):
        errors.append({"source": "economic_calendar", "error": str(econ_events)})
        econ_events = []

    if isinstance(market_overview, Exception):
        errors.append({"source": "market_overview", "error": str(market_overview)})
        market_overview = {"btc_dominance": None, "total_market_cap_change_24h": None}

    raw_symbols = [_normalize_crypto_symbol(s) for s in symbols]

    try:
        tickers = await fetch_multiple_tickers(raw_symbols)
        ticker_map = {t["market"]: t for t in tickers}
    except Exception as exc:
        logger.error(f"Failed to fetch tickers: {exc}")
        errors.append({"source": "tickers", "error": str(exc)})
        ticker_map = {}

    symbol_contexts: list[N8nSymbolContext] = []

    for symbol in symbols:
        raw_symbol = _normalize_crypto_symbol(symbol)
        ticker = ticker_map.get(raw_symbol, {})

        if not ticker:
            errors.append({"symbol": symbol, "error": "Failed to fetch ticker data"})
            continue

        indicators = await _compute_symbol_indicators(symbol)

        if indicators is None:
            errors.append({"symbol": symbol, "error": "Failed to compute indicators"})
            continue

        current_price = indicators["current_price"]
        change_rate = ticker.get("signed_change_rate", 0) * 100
        trade_price_24h = ticker.get("acc_trade_price_24h", 0)

        context = N8nSymbolContext(
            symbol=symbol.upper(),
            raw_symbol=raw_symbol,
            current_price=current_price,
            current_price_fmt=fmt_price(current_price, "KRW"),
            change_24h_pct=round(change_rate, 2) if change_rate else None,
            change_24h_fmt=fmt_gap(change_rate) if change_rate else None,
            volume_24h_krw=trade_price_24h,
            volume_24h_fmt=_fmt_volume_krw(trade_price_24h),
            rsi_14=indicators.get("rsi_14"),
            rsi_7=indicators.get("rsi_7"),
            stoch_rsi_k=indicators.get("stoch_rsi_k"),
            adx=indicators.get("adx"),
            ema_20_distance_pct=indicators.get("ema_20_distance_pct"),
            trend=indicators.get("trend", "neutral"),
            trend_strength=indicators.get("trend_strength", "weak"),
        )
        symbol_contexts.append(context)

    bullish_count = sum(1 for s in symbol_contexts if s.trend == "bullish")
    bearish_count = sum(1 for s in symbol_contexts if s.trend == "bearish")
    neutral_count = sum(1 for s in symbol_contexts if s.trend == "neutral")

    valid_rsis = [s.rsi_14 for s in symbol_contexts if s.rsi_14 is not None]
    avg_rsi = round(sum(valid_rsis) / len(valid_rsis), 1) if valid_rsis else None

    if bullish_count > bearish_count and bullish_count > neutral_count:
        sentiment = "cautiously_bullish"
    elif bearish_count > bullish_count and bearish_count > neutral_count:
        sentiment = "cautiously_bearish"
    else:
        sentiment = "neutral"

    summary = N8nMarketContextSummary(
        total_symbols=len(symbol_contexts),
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        neutral_count=neutral_count,
        avg_rsi=avg_rsi,
        market_sentiment=sentiment,
    )

    fear_greed_model = None
    if fear_greed_data and isinstance(fear_greed_data, dict):
        fear_greed_model = N8nFearGreedData(**fear_greed_data)

    economic_events_models = []
    if econ_events and isinstance(econ_events, list):
        for event in econ_events:
            if isinstance(event, dict):
                economic_events_models.append(N8nEconomicEvent(**event))

    market_overview_obj = N8nMarketOverview(
        fear_greed=fear_greed_model,
        btc_dominance=market_overview.get("btc_dominance"),
        total_market_cap_change_24h=market_overview.get("total_market_cap_change_24h"),
        economic_events_today=economic_events_models,
    )

    return {
        "market_overview": market_overview_obj,
        "symbols": symbol_contexts,
        "summary": summary,
        "errors": errors,
    }
