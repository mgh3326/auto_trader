"""Crypto scan service for n8n integration.

Reuses DailyScanner data-collection logic but returns raw indicator data
without signal judgement, message assembly, or alert delivery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from app.core.config import settings
from app.jobs.daily_scan import DailyScanner
from app.mcp_server.tooling.market_data_indicators import _calculate_rsi, _calculate_sma
from app.services.brokers.upbit.client import (
    fetch_multiple_tickers,
    fetch_my_coins,
    fetch_ohlcv,
    fetch_top_traded_coins,
)
from app.services.external.fear_greed import fetch_fear_greed
from app.services.upbit_symbol_universe_service import get_upbit_korean_name_by_coin

logger = logging.getLogger(__name__)

# Concurrent OHLCV fetch limit (Upbit rate limit aware)
_OHLCV_SEMAPHORE_LIMIT = 5


async def _empty_coins_task() -> list[dict]:
    """Return empty list for when holdings are disabled."""
    return []


def _to_float(value: object) -> float | None:
    """Safe float conversion."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _indicator_value(data: dict[str, float | None], key: str) -> float | None:
    return _to_float(data.get(key))


def _currency_from_market(market: str) -> str:
    if "-" in market:
        return market.split("-")[-1].upper()
    return market.upper()


async def _fetch_coin_name(currency: str) -> str:
    """Get Korean name, returning currency code on failure."""
    try:
        return await get_upbit_korean_name_by_coin(currency, quote_currency="KRW")
    except Exception:
        return currency


def _detect_sma_cross(
    close: pd.Series,
) -> dict[str, Any] | None:
    """Detect SMA20 golden/dead cross from close series.

    Returns dict with type/prev_close/curr_close/prev_sma20/curr_sma20,
    or None if no crossing or insufficient data.
    """
    if len(close) < 21:
        return None

    prev_close = _to_float(close.iloc[-2])
    curr_close = _to_float(close.iloc[-1])
    prev_sma20 = _indicator_value(_calculate_sma(close.iloc[:-1], periods=[20]), "20")
    curr_sma20 = _indicator_value(_calculate_sma(close, periods=[20]), "20")

    if any(v is None for v in (prev_close, curr_close, prev_sma20, curr_sma20)):
        return None

    if prev_close < prev_sma20 and curr_close > curr_sma20:
        return {
            "type": "golden",
            "prev_close": prev_close,
            "curr_close": curr_close,
            "prev_sma20": prev_sma20,
            "curr_sma20": curr_sma20,
        }
    elif prev_close > prev_sma20 and curr_close < curr_sma20:
        return {
            "type": "dead",
            "prev_close": prev_close,
            "curr_close": curr_close,
            "prev_sma20": prev_sma20,
            "curr_sma20": curr_sma20,
        }

    return None


async def _build_btc_context(ohlcv_days: int) -> tuple[dict[str, Any], list[dict]]:
    """Build BTC technical context. Returns (btc_context_dict, errors)."""
    errors: list[dict] = []
    ctx: dict[str, Any] = {
        "rsi14": None,
        "sma20": None,
        "sma60": None,
        "sma200": None,
        "current_price": None,
        "change_rate_24h": None,
    }

    try:
        btc_df = await fetch_ohlcv("KRW-BTC", days=ohlcv_days)
        if not btc_df.empty and "close" in btc_df.columns:
            close = btc_df["close"]
            ctx["rsi14"] = _indicator_value(_calculate_rsi(close), "14")
            sma = _calculate_sma(close, periods=[20, 60, 200])
            ctx["sma20"] = _indicator_value(sma, "20")
            ctx["sma60"] = _indicator_value(sma, "60")
            ctx["sma200"] = _indicator_value(sma, "200")
    except Exception as exc:
        logger.warning("Failed to fetch BTC OHLCV: %s", exc)
        errors.append({"source": "btc_ohlcv", "error": str(exc)})

    try:
        tickers = await fetch_multiple_tickers(["KRW-BTC"])
        if tickers:
            ctx["current_price"] = _to_float(tickers[0].get("trade_price"))
            ctx["change_rate_24h"] = _to_float(tickers[0].get("signed_change_rate"))
    except Exception as exc:
        logger.warning("Failed to fetch BTC ticker: %s", exc)
        errors.append({"source": "btc_ticker", "error": str(exc)})

    return ctx, errors


async def _build_coin_data(
    *,
    market: str,
    rank: int | None,
    is_holding: bool,
    ticker_map: dict[str, dict],
    ohlcv_days: int,
    include_crash: bool,
    include_sma_cross: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, Any] | None, list[dict]]:
    """Build scan data for a single coin. Returns (coin_dict, errors)."""
    errors: list[dict] = []
    currency = _currency_from_market(market)
    name = await _fetch_coin_name(currency)

    ticker = ticker_map.get(market, {})
    current_price = _to_float(ticker.get("trade_price"))
    change_rate_24h = _to_float(ticker.get("signed_change_rate"))
    trade_amount_24h = _to_float(ticker.get("acc_trade_price_24h"))

    indicators: dict[str, float | None] = {
        "rsi14": None,
        "sma20": None,
        "sma60": None,
        "sma200": None,
    }
    sma_cross = None
    crash = None

    # Fetch OHLCV with concurrency limit
    try:
        async with semaphore:
            df = await fetch_ohlcv(market, days=ohlcv_days)

        if not df.empty and "close" in df.columns:
            close = df["close"]
            indicators["rsi14"] = _indicator_value(_calculate_rsi(close), "14")
            sma = _calculate_sma(close, periods=[20, 60, 200])
            indicators["sma20"] = _indicator_value(sma, "20")
            indicators["sma60"] = _indicator_value(sma, "60")
            indicators["sma200"] = _indicator_value(sma, "200")

            if include_sma_cross:
                sma_cross = _detect_sma_cross(close)
    except Exception as exc:
        logger.warning("Failed to fetch OHLCV for %s: %s", market, exc)
        errors.append({"source": f"ohlcv:{market}", "error": str(exc)})

    # Crash detection
    if include_crash and change_rate_24h is not None:
        threshold = DailyScanner._crash_threshold_for_candidate(rank, is_holding)
        crash = {
            "change_rate_24h": change_rate_24h,
            "threshold": threshold,
            "triggered": abs(change_rate_24h) >= threshold,
        }

    coin = {
        "symbol": market,
        "currency": currency,
        "name": name,
        "rank": rank,
        "is_holding": is_holding,
        "current_price": current_price,
        "change_rate_24h": change_rate_24h,
        "trade_amount_24h": trade_amount_24h,
        "indicators": indicators,
        "sma_cross": sma_cross,
        "crash": crash,
    }

    return coin, errors


async def fetch_crypto_scan(
    *,
    top_n: int = 30,
    include_holdings: bool = True,
    include_crash: bool = True,
    include_sma_cross: bool = True,
    include_fear_greed: bool = True,
    ohlcv_days: int = 50,
) -> dict[str, Any]:
    """Collect crypto scan data: indicators, crash, SMA cross, F&G.

    Returns a dict matching N8nCryptoScanResponse schema.
    Does NOT make signal judgements or send alerts.
    """
    errors: list[dict] = []

    # 1. Fetch top traded coins and holdings in parallel
    top_coins_task = fetch_top_traded_coins("KRW")
    my_coins_task = fetch_my_coins() if include_holdings else _empty_coins_task()

    try:
        top_coins, my_coins = await asyncio.gather(
            top_coins_task, my_coins_task, return_exceptions=False
        )
    except Exception as exc:
        logger.error("Failed to fetch coin universe: %s", exc)
        return {
            "success": False,
            "btc_context": {},
            "fear_greed": None,
            "coins": [],
            "summary": {
                "total_scanned": 0,
                "top_n_count": 0,
                "holdings_added": 0,
            },
            "errors": [{"source": "universe", "error": str(exc)}],
        }

    # 2. Build rank map and determine scan universe
    rank_by_market = DailyScanner._build_rank_by_market(top_coins)

    # Top N markets
    top_n_markets: set[str] = set()
    for market, rank in rank_by_market.items():
        if rank <= top_n:
            top_n_markets.add(market)

    # Holdings markets (outside top N)
    holding_markets: set[str] = set()
    if include_holdings:
        for coin in my_coins:
            currency = str(coin.get("currency") or "").upper()
            if not currency or currency == "KRW":
                continue
            market = f"KRW-{currency}"
            # Add to holding_markets if valid currency (even if not in top traded)
            holding_markets.add(market)

    holdings_added = len(holding_markets - top_n_markets)
    all_markets = sorted(top_n_markets | holding_markets)

    # 3. Fetch tickers, BTC context, and F&G in parallel
    fg_task = fetch_fear_greed() if include_fear_greed else None

    try:
        ticker_result = await fetch_multiple_tickers(all_markets)
    except Exception as exc:
        logger.warning("Failed to fetch tickers: %s", exc)
        ticker_result = []
        errors.append({"source": "tickers", "error": str(exc)})

    ticker_map = {t["market"]: t for t in ticker_result if "market" in t}
    btc_context, btc_errors = await _build_btc_context(ohlcv_days)
    errors.extend(btc_errors)

    fear_greed_data = None
    if fg_task is not None:
        try:
            fear_greed_data = await fg_task
        except Exception as exc:
            logger.warning("Failed to fetch Fear & Greed: %s", exc)
            errors.append({"source": "fear_greed", "error": str(exc)})

    # 4. Build coin data in parallel with semaphore
    semaphore = asyncio.Semaphore(_OHLCV_SEMAPHORE_LIMIT)
    coin_tasks = []
    for market in all_markets:
        rank = rank_by_market.get(market)
        is_holding = market in holding_markets
        # rank is None for holding-only coins not in top N
        display_rank = rank if market in top_n_markets else None

        coin_tasks.append(
            _build_coin_data(
                market=market,
                rank=display_rank,
                is_holding=is_holding,
                ticker_map=ticker_map,
                ohlcv_days=ohlcv_days,
                include_crash=include_crash,
                include_sma_cross=include_sma_cross,
                semaphore=semaphore,
            )
        )

    coin_results = await asyncio.gather(*coin_tasks, return_exceptions=True)

    # 5. Collect results
    coins: list[dict] = []
    for result in coin_results:
        if isinstance(result, Exception):
            errors.append({"source": "coin_build", "error": str(result)})
            continue
        coin_data, coin_errors = result
        if coin_data is not None:
            coins.append(coin_data)
        errors.extend(coin_errors)

    # 6. Sort by RSI ascending (null RSI last)
    def rsi_sort_key(coin: dict) -> tuple[int, float]:
        rsi = coin["indicators"]["rsi14"]
        if rsi is None:
            return (1, 0.0)  # null RSI goes last
        return (0, rsi)

    coins.sort(key=rsi_sort_key)

    # 7. Build summary
    oversold_count = sum(
        1
        for c in coins
        if c["indicators"]["rsi14"] is not None
        and c["indicators"]["rsi14"] < settings.DAILY_SCAN_RSI_OVERSOLD
    )
    overbought_count = sum(
        1
        for c in coins
        if c["indicators"]["rsi14"] is not None
        and c["indicators"]["rsi14"] > settings.DAILY_SCAN_RSI_OVERBOUGHT
    )
    crash_triggered_count = sum(
        1 for c in coins if c.get("crash") and c["crash"]["triggered"]
    )
    sma_golden = sum(
        1 for c in coins if c.get("sma_cross") and c["sma_cross"]["type"] == "golden"
    )
    sma_dead = sum(
        1 for c in coins if c.get("sma_cross") and c["sma_cross"]["type"] == "dead"
    )

    return {
        "success": True,
        "btc_context": btc_context,
        "fear_greed": fear_greed_data,
        "coins": coins,
        "summary": {
            "total_scanned": len(coins),
            "top_n_count": len(top_n_markets),
            "holdings_added": holdings_added,
            "oversold_count": oversold_count,
            "overbought_count": overbought_count,
            "crash_triggered_count": crash_triggered_count,
            "sma_golden_cross_count": sma_golden,
            "sma_dead_cross_count": sma_dead,
        },
        "errors": errors,
    }
