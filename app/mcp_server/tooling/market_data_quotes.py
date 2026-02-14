"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot, Fibonacci).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    normalize_market as _normalize_market,
    normalize_rows as _normalize_rows,
    normalize_symbol_input as _normalize_symbol_input,
    resolve_market_type as _resolve_market_type,
    to_float as _to_float,
    to_optional_float as _to_optional_float,
)
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient
from data.coins_info import get_or_refresh_maps
from data.stocks_info import (
    get_kosdaq_name_to_code,
    get_kospi_name_to_code,
    get_us_stocks_data,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Type Definitions and Constants
# ---------------------------------------------------------------------------

IndicatorType = Literal["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]

DEFAULT_SMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_EMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_RSI_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_STD = 2.0
DEFAULT_ATR_PERIOD = 14

FIBONACCI_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


# ---------------------------------------------------------------------------
# Symbol Search
# ---------------------------------------------------------------------------


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """Search symbols across KRX, US, and Upbit master datasets."""
    results: list[dict[str, Any]] = []
    query_lower = query.lower()
    query_upper = query.upper()

    if instrument_type is None or instrument_type == "equity_kr":
        kospi = get_kospi_name_to_code()
        kosdaq = get_kosdaq_name_to_code()

        for name, code in kospi.items():
            if query_lower in name.lower() or query_upper in code:
                results.append(
                    {
                        "symbol": code,
                        "name": name,
                        "instrument_type": "equity_kr",
                        "exchange": "KOSPI",
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

        for name, code in kosdaq.items():
            if query_lower in name.lower() or query_upper in code:
                results.append(
                    {
                        "symbol": code,
                        "name": name,
                        "instrument_type": "equity_kr",
                        "exchange": "KOSDAQ",
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

    if instrument_type is None or instrument_type == "equity_us":
        us_data = get_us_stocks_data()
        symbol_to_exchange = us_data.get("symbol_to_exchange", {})
        symbol_to_name_kr = us_data.get("symbol_to_name_kr", {})
        symbol_to_name_en = us_data.get("symbol_to_name_en", {})

        for symbol, exchange in symbol_to_exchange.items():
            name_kr = symbol_to_name_kr.get(symbol, "")
            name_en = symbol_to_name_en.get(symbol, "")
            if (
                query_upper in symbol.upper()
                or query_lower in name_kr.lower()
                or query_lower in name_en.lower()
            ):
                results.append(
                    {
                        "symbol": symbol,
                        "name": name_kr or name_en or symbol,
                        "instrument_type": "equity_us",
                        "exchange": exchange,
                        "is_active": True,
                    }
                )
                if len(results) >= limit:
                    return results

    if instrument_type is None or instrument_type == "crypto":
        try:
            crypto_maps = await get_or_refresh_maps()
            name_to_pair = crypto_maps.get("NAME_TO_PAIR_KR", {})
            for name, pair in name_to_pair.items():
                if query_lower in name.lower() or query_upper in pair.upper():
                    results.append(
                        {
                            "symbol": pair,
                            "name": name,
                            "instrument_type": "crypto",
                            "exchange": "Upbit",
                            "is_active": True,
                        }
                    )
                    if len(results) >= limit:
                        return results
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Quote Fetching
# ---------------------------------------------------------------------------


async def _fetch_quote_crypto(symbol: str) -> dict[str, Any]:
    """Fetch crypto quote from Upbit."""
    prices = await upbit_service.fetch_multiple_current_prices([symbol])
    price = prices.get(symbol)
    if price is None:
        raise ValueError(f"Symbol '{symbol}' not found")
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "price": price,
        "source": "upbit",
    }


async def _fetch_quote_equity_kr(symbol: str) -> dict[str, Any]:
    """Fetch Korean equity quote from KIS."""
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol,
        market="J",
        n=1,  # J = 주식/ETF/ETN
    )
    if df.empty:
        raise ValueError(f"Symbol '{symbol}' not found")
    last = df.iloc[-1].to_dict()
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": last.get("close"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }


async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    """Fetch US equity quote from Yahoo Finance."""
    import yfinance as yf

    from app.core.symbol import to_yahoo_symbol

    yahoo_ticker = to_yahoo_symbol(symbol)
    info = yf.Ticker(yahoo_ticker).fast_info

    price = getattr(info, "last_price", None)
    if price is None:
        raise ValueError(f"Symbol '{symbol}' not found")

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": getattr(info, "regular_market_previous_close", None),
        "open": getattr(info, "open", None),
        "high": getattr(info, "day_high", None),
        "low": getattr(info, "day_low", None),
        "volume": getattr(info, "last_volume", None),
        "source": "yahoo",
    }


# ---------------------------------------------------------------------------
# OHLCV Fetching
# ---------------------------------------------------------------------------


async def _fetch_ohlcv_crypto(
    symbol: str, count: int, period: str, end_date: datetime.datetime | None
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=end_date
    )

    if df.empty:
        return {
            "symbol": symbol,
            "instrument_type": "crypto",
            "source": "upbit",
            "period": period,
            "count": 0,
            "rows": [],
            "message": f"No candle data available for {symbol}",
        }

    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "source": "upbit",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
) -> dict[str, Any]:
    """Fetch Korean equity OHLCV from KIS."""
    capped_count = min(count, 200)
    # KIS uses D/W/M for period
    kis_period_map = {"day": "D", "week": "W", "month": "M"}
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol,
        market="J",  # J = 주식/ETF/ETN
        n=capped_count,
        period=kis_period_map.get(period, "D"),
        end_date=end_date.date() if end_date else None,
    )
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_us(
    symbol: str, count: int, period: str, end_date: datetime.datetime | None
) -> dict[str, Any]:
    """Fetch US equity OHLCV from Yahoo Finance."""
    capped_count = min(count, 100)
    df = await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=capped_count, period=period, end_date=end_date
    )
    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "yahoo",
        "period": period,
        "count": capped_count,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_crypto_paginated(
    symbol: str, count: int, period: str = "day"
) -> pd.DataFrame:
    """Fetch crypto OHLCV with pagination to overcome Upbit's 200 limit.

    Args:
        symbol: Market symbol (e.g., "KRW-BTC")
        count: Total number of candles to fetch
        period: Candle period ("day", "week", "month")

    Returns:
        DataFrame with requested number of candles
    """
    max_per_request = 200
    all_dfs: list[pd.DataFrame] = []
    remaining = count
    end_date: datetime.datetime | None = None

    while remaining > 0:
        batch_size = min(remaining, max_per_request)
        df_batch = await upbit_service.fetch_ohlcv(
            market=symbol, days=batch_size, period=period, end_date=end_date
        )

        if df_batch.empty:
            break

        all_dfs.append(df_batch)
        remaining -= len(df_batch)

        if remaining > 0 and len(df_batch) > 0:
            # Get the earliest date from this batch for next pagination
            earliest_date = df_batch["date"].min()
            # Set end_date to the day before the earliest date
            end_date = datetime.datetime.combine(
                earliest_date - datetime.timedelta(days=1),
                datetime.time(23, 59, 59),
            )

    if not all_dfs:
        return pd.DataFrame()

    # Concatenate all batches, sort by date, and remove duplicates
    combined = pd.concat(all_dfs, ignore_index=True)
    combined = (
        combined.drop_duplicates(subset=["date"])
        .sort_values("date")
        .reset_index(drop=True)
    )

    return combined


async def _fetch_ohlcv_for_indicators(
    symbol: str, market_type: str, count: int = 250
) -> pd.DataFrame:
    """Fetch OHLCV data for indicator calculation.

    Fetches enough data for long-term indicators (200-day SMA needs 200+ candles).
    """
    if market_type == "crypto":
        # Use pagination for crypto to overcome Upbit's 200 limit
        df = await _fetch_ohlcv_crypto_paginated(symbol, count=count, period="day")
    elif market_type == "equity_kr":
        capped_count = min(count, 250)
        kis = KISClient()
        df = await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=capped_count, period="D"
        )
    else:  # equity_us
        capped_count = min(count, 250)
        df = await yahoo_service.fetch_ohlcv(
            ticker=symbol, days=capped_count, period="day"
        )

    return df


async def _fetch_ohlcv_for_volume_profile(
    symbol: str, market_type: str, period_days: int
) -> pd.DataFrame:
    """Fetch daily OHLCV data for volume profile analysis."""
    if market_type == "crypto":
        return await _fetch_ohlcv_crypto_paginated(
            symbol=symbol, count=period_days, period="day"
        )
    if market_type == "equity_kr":
        kis = KISClient()
        return await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=period_days, period="D"
        )
    return await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=period_days, period="day"
    )


# ---------------------------------------------------------------------------
# Technical Indicator Calculations
# ---------------------------------------------------------------------------


def _calculate_sma(
    close: pd.Series, periods: list[int] | None = None
) -> dict[str, float | None]:
    """Calculate Simple Moving Average for multiple periods."""
    periods = periods or DEFAULT_SMA_PERIODS
    result: dict[str, float | None] = {}
    for period in periods:
        if len(close) >= period:
            sma_value = close.iloc[-period:].mean()
            result[str(period)] = float(sma_value) if pd.notna(sma_value) else None
        else:
            result[str(period)] = None
    return result


def _calculate_ema(
    close: pd.Series, periods: list[int] | None = None
) -> dict[str, float | None]:
    """Calculate Exponential Moving Average for multiple periods."""
    periods = periods or DEFAULT_EMA_PERIODS
    result: dict[str, float | None] = {}
    for period in periods:
        if len(close) >= period:
            ema = close.ewm(span=period, adjust=False).mean()
            ema_value = ema.iloc[-1]
            result[str(period)] = float(ema_value) if pd.notna(ema_value) else None
        else:
            result[str(period)] = None
    return result


def _calculate_rsi(
    close: pd.Series, period: int = DEFAULT_RSI_PERIOD
) -> dict[str, float | None]:
    """Calculate Relative Strength Index."""
    if len(close) < period + 1:
        return {str(period): None}

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    rsi_value = rsi.iloc[-1]
    return {str(period): round(float(rsi_value), 2) if pd.notna(rsi_value) else None}


def _calculate_macd(
    close: pd.Series,
    fast: int = DEFAULT_MACD_FAST,
    slow: int = DEFAULT_MACD_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> dict[str, float | None]:
    """Calculate MACD, Signal, and Histogram."""
    if len(close) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    macd_val = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    hist_val = histogram.iloc[-1]

    return {
        "macd": float(macd_val) if pd.notna(macd_val) else None,
        "signal": float(signal_val) if pd.notna(signal_val) else None,
        "histogram": float(hist_val) if pd.notna(hist_val) else None,
    }


def _calculate_bollinger(
    close: pd.Series,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    std: float = DEFAULT_BOLLINGER_STD,
) -> dict[str, float | None]:
    """Calculate Bollinger Bands (upper, middle, lower)."""
    if len(close) < period:
        return {"upper": None, "middle": None, "lower": None}

    sma = close.rolling(window=period).mean()
    rolling_std = close.rolling(window=period).std()

    upper = sma + (rolling_std * std)
    lower = sma - (rolling_std * std)

    sma_val = sma.iloc[-1]
    upper_val = upper.iloc[-1]
    lower_val = lower.iloc[-1]

    return {
        "upper": float(upper_val) if pd.notna(upper_val) else None,
        "middle": float(sma_val) if pd.notna(sma_val) else None,
        "lower": float(lower_val) if pd.notna(lower_val) else None,
    }


def _calculate_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = DEFAULT_ATR_PERIOD
) -> dict[str, float | None]:
    """Calculate Average True Range."""
    if len(close) < period + 1:
        return {str(period): None}

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    atr_value = atr.iloc[-1]

    return {str(period): float(atr_value) if pd.notna(atr_value) else None}


def _calculate_pivot(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> dict[str, float | None]:
    """Calculate Pivot Points (classic) based on previous day's HLC."""
    if len(close) < 2:
        return {
            "p": None,
            "r1": None,
            "r2": None,
            "r3": None,
            "s1": None,
            "s2": None,
            "s3": None,
        }

    # Use previous day's data
    prev_high = float(high.iloc[-2])
    prev_low = float(low.iloc[-2])
    prev_close = float(close.iloc[-2])

    # Classic pivot point formula
    p = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * p - prev_low
    r2 = p + (prev_high - prev_low)
    r3 = prev_high + 2 * (p - prev_low)
    s1 = 2 * p - prev_high
    s2 = p - (prev_high - prev_low)
    s3 = prev_low - 2 * (prev_high - p)

    return {
        "p": round(p, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "r3": round(r3, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "s3": round(s3, 2),
    }


def _calculate_fibonacci(df: pd.DataFrame, current_price: float) -> dict[str, Any]:
    """Calculate Fibonacci retracement levels from OHLCV DataFrame.

    Detects swing high/low, determines trend direction, and computes
    retracement levels with nearest support/resistance.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    swing_high_price = round(float(high.max()), 2)
    swing_low_price = round(float(low.min()), 2)

    # Use positional index for ordering comparison
    swing_high_pos = int(high.values.argmax())
    swing_low_pos = int(low.values.argmin())

    # Determine dates
    def _to_date_str(row: pd.Series) -> str:
        d = row.get("date")
        if d is None:
            return ""
        if isinstance(d, str):
            return d[:10]
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            return d.strftime("%Y-%m-%d")
        return str(d)[:10]

    swing_high_date = _to_date_str(df.iloc[swing_high_pos])
    swing_low_date = _to_date_str(df.iloc[swing_low_pos])

    # Trend: if high came after low → retracement from high, else bounce from low
    if swing_high_pos > swing_low_pos:
        trend = "retracement_from_high"
        # Levels go from high (0%) down to low (100%)
        levels = {
            str(lvl): round(
                swing_high_price - lvl * (swing_high_price - swing_low_price), 2
            )
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        # Levels go from low (0%) up to high (100%)
        levels = {
            str(lvl): round(
                swing_low_price + lvl * (swing_high_price - swing_low_price), 2
            )
            for lvl in FIBONACCI_LEVELS
        }

    # Find nearest support (level price just below current) and resistance (just above)
    nearest_support: dict[str, Any] | None = None
    nearest_resistance: dict[str, Any] | None = None

    sorted_levels = sorted(levels.items(), key=lambda x: x[1])
    for level_str, price in sorted_levels:
        if price < current_price:
            nearest_support = {"level": level_str, "price": price}
        elif price > current_price and nearest_resistance is None:
            nearest_resistance = {"level": level_str, "price": price}

    return {
        "swing_high": {"price": swing_high_price, "date": swing_high_date},
        "swing_low": {"price": swing_low_price, "date": swing_low_date},
        "trend": trend,
        "current_price": current_price,
        "levels": levels,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def _compute_indicators(
    df: pd.DataFrame, indicators: list[IndicatorType]
) -> dict[str, dict[str, float | None]]:
    """Compute requested indicators from OHLCV DataFrame.

    Args:
        df: DataFrame with columns: open, high, low, close, volume
        indicators: List of indicator types to compute

    Returns:
        Dictionary with indicator results
    """
    results: dict[str, dict[str, float | None]] = {}

    # Ensure we have required columns
    required = {"close"}
    if "atr" in indicators or "pivot" in indicators:
        required |= {"high", "low"}

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else None
    low = df["low"].astype(float) if "low" in df.columns else None

    for indicator in indicators:
        if indicator == "sma":
            results["sma"] = _calculate_sma(close)
        elif indicator == "ema":
            results["ema"] = _calculate_ema(close)
        elif indicator == "rsi":
            results["rsi"] = _calculate_rsi(close)
        elif indicator == "macd":
            results["macd"] = _calculate_macd(close)
        elif indicator == "bollinger":
            results["bollinger"] = _calculate_bollinger(close)
        elif indicator == "atr":
            if high is not None and low is not None:
                results["atr"] = _calculate_atr(high, low, close)
            else:
                results["atr"] = {str(DEFAULT_ATR_PERIOD): None}
        elif indicator == "pivot":
            if high is not None and low is not None:
                results["pivot"] = _calculate_pivot(high, low, close)

    return results


# ---------------------------------------------------------------------------
# Support/Resistance Level Helpers
# ---------------------------------------------------------------------------


def _format_fibonacci_source(level_key: str) -> str:
    level = _to_optional_float(level_key)
    if level is None:
        return f"fib_{level_key}"

    pct = level * 100
    if abs(pct - round(pct)) < 1e-9:
        pct_str = str(int(round(pct)))
    else:
        pct_str = f"{pct:.1f}".rstrip("0").rstrip(".")

    return f"fib_{pct_str}"


def _cluster_price_levels(
    levels: list[tuple[float, str]],
    tolerance_pct: float = 0.02,
) -> list[dict[str, Any]]:
    if not levels:
        return []

    clusters: list[dict[str, Any]] = []
    for price, source in sorted(levels, key=lambda item: item[0]):
        if price <= 0:
            continue

        matched_cluster: dict[str, Any] | None = None
        for cluster in clusters:
            center = _to_float(cluster.get("center"), default=0.0)
            if center <= 0:
                continue
            if abs(price - center) / center <= tolerance_pct:
                matched_cluster = cluster
                break

        if matched_cluster is None:
            clusters.append(
                {
                    "prices": [price],
                    "sources": [source],
                    "center": price,
                }
            )
            continue

        prices = matched_cluster["prices"]
        sources = matched_cluster["sources"]
        prices.append(price)
        if source not in sources:
            sources.append(source)
        matched_cluster["center"] = sum(prices) / len(prices)

    clustered: list[dict[str, Any]] = []
    for cluster in clusters:
        prices = cluster.get("prices", [])
        if not prices:
            continue

        level_sources = cluster.get("sources", [])
        source_count = len(level_sources)
        if source_count >= 3:
            strength = "strong"
        elif source_count == 2:
            strength = "moderate"
        else:
            strength = "weak"

        clustered.append(
            {
                "price": round(sum(prices) / len(prices), 2),
                "strength": strength,
                "sources": level_sources,
            }
        )

    return clustered


def _split_support_resistance_levels(
    clustered_levels: list[dict[str, Any]],
    current_price: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    supports: list[dict[str, Any]] = []
    resistances: list[dict[str, Any]] = []

    for level in clustered_levels:
        price = _to_float(level.get("price"), default=0.0)
        if price <= 0:
            continue
        level["distance_pct"] = round((price - current_price) / current_price * 100, 2)
        if price < current_price:
            supports.append(level)
        elif price > current_price:
            resistances.append(level)

    supports.sort(
        key=lambda item: _to_float(item.get("price"), default=0.0), reverse=True
    )
    resistances.sort(key=lambda item: _to_float(item.get("price"), default=0.0))
    return supports, resistances


# ---------------------------------------------------------------------------
# DCA Price Level Helpers
# ---------------------------------------------------------------------------


def _compute_rsi_weights(rsi_value: float | None, splits: int) -> list[float]:
    """Compute DCA weight distribution based on RSI value.

    Args:
        rsi_value: RSI 14 value (0-100). None means no RSI data.
        splits: Number of DCA splits (e.g., 3 for 3-step buying)

    Returns:
        List of weights that sum to 1.0. RSI < 30 gives higher weight
        to early steps (linear decreasing), RSI > 50 gives higher weight
        to later steps (linear increasing), 30-50 or None gives equal weights.
    """
    if rsi_value is None:
        # No RSI data: equal distribution
        return [1.0 / splits] * splits

    if rsi_value < 30:
        # Oversold: front-weighted (buy more at early/closer steps)
        # splits=3: raw=[3,2,1] → [0.5, 0.333, 0.167]
        raw = [splits - i for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    elif rsi_value > 50:
        # Overbought: back-weighted (buy more at later/lower steps)
        # splits=3: raw=[1,2,3] → [0.167, 0.333, 0.5]
        raw = [i + 1 for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    else:
        # Neutral (30-50): equal distribution
        return [1.0 / splits] * splits


def _compute_dca_price_levels(
    strategy: str,
    splits: int,
    current_price: float,
    supports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute DCA price levels based on strategy and support levels.

    Args:
        strategy: "support", "equal", or "aggressive"
        splits: Number of price levels to generate
        current_price: Current market price
        supports: List of support levels from get_support_resistance

    Returns:
        List of dicts with "price" and "source" keys, sorted by price
        descending (closest to current price first).
    """
    support_prices = sorted(
        [_to_float(level.get("price")) for level in supports if level.get("price")],
        reverse=True,  # Closest to current price first
    )

    if strategy == "support":
        # Use closest support levels, fill gaps with interpolation
        if len(support_prices) >= splits:
            # Enough supports: use closest splits
            return [
                {"price": price, "source": "support"}
                for price in support_prices[:splits]
            ]
        elif len(support_prices) > 0:
            # Fewer supports: interpolate between them
            support_levels: list[dict[str, Any]] = []
            start_price = current_price * 0.995
            end_price = min(support_prices)
            step = (end_price - start_price) / (splits - 1)
            used_supports: set[float] = set()
            for i in range(splits):
                price = start_price + step * i
                # Check if any support is near this price (within 2%)
                near_support = None
                for supp in support_prices:
                    if supp in used_supports:
                        continue
                    if abs(price - supp) / price < 0.02:
                        near_support = supp
                        break
                if near_support is not None:
                    price = near_support
                    used_supports.add(near_support)
                support_levels.append({"price": price, "source": "support"})
            return support_levels
        else:
            # No supports: synthetic levels (-2%, -4%, -6%...)
            return [
                {
                    "price": current_price * (1.0 - 0.02 * (i + 1)),
                    "source": "synthetic",
                }
                for i in range(splits)
            ]

    elif strategy == "equal":
        # Equal spacing between current_price and lowest support
        if support_prices:
            min_price = min(support_prices)
        else:
            # No supports: go down to -10%
            min_price = current_price * 0.90

        start_price = current_price * 0.995
        step = (min_price - start_price) / (splits - 1)
        return [
            {"price": start_price + step * i, "source": "equal_spaced"}
            for i in range(splits)
        ]

    elif strategy == "aggressive":
        first_price = current_price * 0.995
        levels: list[dict[str, Any]] = [
            {"price": first_price, "source": "aggressive_first"}
        ]

        if splits <= 1:
            return levels

        support_prices = [s["price"] for s in supports]
        if support_prices:
            end_price = min(support_prices)
        else:
            end_price = current_price * 0.98

        remaining = splits - 1
        used_supports: set[float] = set()

        if len(support_prices) >= remaining:
            for i in range(1, splits):
                price = first_price + ((end_price - first_price) / (splits - 1)) * i
                near_support = None
                for supp in support_prices:
                    if supp in used_supports:
                        continue
                    if abs(price - supp) / price < 0.02 and supp < price:
                        near_support = supp
                        break
                if near_support is not None:
                    price = near_support
                    used_supports.add(near_support)
                source = "support" if near_support else "interpolated"
                levels.append({"price": price, "source": source})
        else:
            step = (end_price - first_price) / remaining
            for i in range(1, splits):
                price = first_price + step * i
                levels.append({"price": price, "source": "interpolated"})

        return levels

    else:
        raise ValueError(
            f"Invalid strategy: {strategy}. Must be 'support', 'equal', or 'aggressive'"
        )


# ---------------------------------------------------------------------------
# Volume Profile Calculations
# ---------------------------------------------------------------------------


def _normalize_number(value: float, decimals: int = 6) -> float | int:
    """Normalize float output to readable numeric values."""
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


def _calculate_volume_profile(
    df: pd.DataFrame,
    bins: int,
    value_area_ratio: float = 0.70,
) -> dict[str, Any]:
    """Calculate price-by-volume distribution from OHLCV candles."""
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if not 0 < value_area_ratio <= 1:
        raise ValueError("value_area_ratio must be between 0 and 1")

    required = {"low", "high", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if df.empty:
        raise ValueError("No OHLCV data available")

    low = pd.to_numeric(df["low"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")

    valid_mask = (~low.isna()) & (~high.isna()) & (~volume.isna())
    if not valid_mask.any():
        raise ValueError("No valid OHLCV rows with low/high/volume")

    low_values = low[valid_mask].astype(float).to_numpy()
    high_values = high[valid_mask].astype(float).to_numpy()
    candle_low = np.minimum(low_values, high_values)
    candle_high = np.maximum(low_values, high_values)
    candle_volume = volume[valid_mask].astype(float).to_numpy()

    price_low = float(candle_low.min())
    price_high = float(candle_high.max())

    if price_high <= price_low:
        # Flat-price edge case: make a tiny synthetic range for binning math.
        epsilon = max(abs(price_low) * 1e-6, 1e-6)
        bin_edges = np.linspace(
            price_low - epsilon / 2,
            price_high + epsilon / 2,
            bins + 1,
        )
    else:
        bin_edges = np.linspace(price_low, price_high, bins + 1)

    bin_volumes = np.zeros(bins, dtype=float)

    for low_i, high_i, vol_i in zip(
        candle_low, candle_high, candle_volume, strict=False
    ):
        if vol_i <= 0:
            continue

        if high_i <= low_i:
            idx = int(
                np.clip(
                    np.searchsorted(bin_edges, low_i, side="right") - 1, 0, bins - 1
                )
            )
            bin_volumes[idx] += vol_i
            continue

        overlaps = np.minimum(bin_edges[1:], high_i) - np.maximum(bin_edges[:-1], low_i)
        overlaps = np.clip(overlaps, 0.0, None)
        overlap_sum = float(overlaps.sum())

        if overlap_sum <= 0:
            mid_price = (low_i + high_i) / 2
            idx = int(
                np.clip(
                    np.searchsorted(bin_edges, mid_price, side="right") - 1,
                    0,
                    bins - 1,
                )
            )
            bin_volumes[idx] += vol_i
            continue

        bin_volumes += vol_i * (overlaps / overlap_sum)

    total_volume = float(bin_volumes.sum())
    if total_volume <= 0:
        raise ValueError("Total volume is zero for the selected period")

    bin_volume_pct = (bin_volumes / total_volume) * 100
    poc_index = int(np.argmax(bin_volumes))

    target_volume = total_volume * value_area_ratio
    covered_volume = float(bin_volumes[poc_index])
    left_index = poc_index
    right_index = poc_index

    while covered_volume < target_volume and (left_index > 0 or right_index < bins - 1):
        left_vol = bin_volumes[left_index - 1] if left_index > 0 else -np.inf
        right_vol = bin_volumes[right_index + 1] if right_index < bins - 1 else -np.inf

        if right_vol > left_vol:
            right_index += 1
            covered_volume += float(bin_volumes[right_index])
        else:
            if left_index > 0:
                left_index -= 1
                covered_volume += float(bin_volumes[left_index])
            elif right_index < bins - 1:
                right_index += 1
                covered_volume += float(bin_volumes[right_index])
            else:
                break

    profile = [
        {
            "price_low": _normalize_number(bin_edges[idx], decimals=6),
            "price_high": _normalize_number(bin_edges[idx + 1], decimals=6),
            "volume": _normalize_number(bin_volumes[idx], decimals=2),
            "volume_pct": _normalize_number(bin_volume_pct[idx], decimals=2),
        }
        for idx in range(bins)
    ]

    return {
        "price_range": {
            "low": _normalize_number(price_low, decimals=6),
            "high": _normalize_number(price_high, decimals=6),
        },
        "poc": {
            "price": _normalize_number(
                (bin_edges[poc_index] + bin_edges[poc_index + 1]) / 2,
                decimals=6,
            ),
            "volume": _normalize_number(bin_volumes[poc_index], decimals=2),
        },
        "value_area": {
            "high": _normalize_number(bin_edges[right_index + 1], decimals=6),
            "low": _normalize_number(bin_edges[left_index], decimals=6),
            "volume_pct": _normalize_number(
                (covered_volume / total_volume) * 100, decimals=2
            ),
        },
        "profile": profile,
    }


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

MARKET_DATA_TOOL_NAMES: set[str] = {
    "search_symbol",
    "get_quote",
    "get_ohlcv",
    "get_indicators",
}


def _register_market_data_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol",
        description=(
            "Search symbols by query (symbol or name). Use market to filter: "
            "kr/kospi/kosdaq (Korean stocks), us/nasdaq/nyse (US stocks), "
            "crypto/upbit (cryptocurrencies)."
        ),
    )
    async def search_symbol(
        query: str, limit: int = 20, market: str | None = None
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        instrument_type = _normalize_market(market)

        try:
            capped_limit = min(max(limit, 1), 100)
            return await _search_master_data(query, capped_limit, instrument_type)
        except Exception as exc:
            return [_error_payload(source="master", message=str(exc), query=query)]

    @mcp.tool(
        name="get_quote",
        description="Get latest quote/last price for a symbol (KR equity / US equity / crypto).",
    )
    async def get_quote(symbol: str | int, market: str | None = None) -> dict[str, Any]:
        symbol = _normalize_symbol_input(symbol, market)
        if not symbol:
            raise ValueError("symbol is required")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_quote_crypto(symbol)
            if market_type == "equity_kr":
                return await _fetch_quote_equity_kr(symbol)
            return await _fetch_quote_equity_us(symbol)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_ohlcv",
        description=(
            "Get OHLCV candles for a symbol. Supports daily/weekly/monthly periods "
            "and date-based pagination."
        ),
    )
    async def get_ohlcv(
        symbol: str,
        count: int = 100,
        period: str = "day",
        end_date: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        count = int(count)
        if count <= 0:
            raise ValueError("count must be > 0")

        period = (period or "day").strip().lower()
        if period not in ("day", "week", "month"):
            raise ValueError("period must be 'day', 'week', or 'month'")

        parsed_end_date: datetime.datetime | None = None
        if end_date:
            try:
                parsed_end_date = datetime.datetime.fromisoformat(end_date)
            except ValueError as exc:
                raise ValueError(
                    "end_date must be ISO format (e.g., '2024-01-15')"
                ) from exc

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_ohlcv_crypto(symbol, count, period, parsed_end_date)
            if market_type == "equity_kr":
                return await _fetch_ohlcv_equity_kr(
                    symbol, count, period, parsed_end_date
                )
            return await _fetch_ohlcv_equity_us(symbol, count, period, parsed_end_date)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    async def _get_indicators_impl(
        symbol: str, indicators: list[str], market: str | None = None
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not indicators:
            raise ValueError("indicators list is required and cannot be empty")

        valid_indicators: set[IndicatorType] = {
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger",
            "atr",
            "pivot",
        }
        normalized_indicators: list[IndicatorType] = []
        for ind in indicators:
            ind_lower = ind.lower().strip()
            if ind_lower not in valid_indicators:
                raise ValueError(
                    f"Invalid indicator '{ind}'. Valid options: {', '.join(sorted(valid_indicators))}"
                )
            normalized_indicators.append(ind_lower)  # type: ignore[arg-type]

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")

            current_price = float(df["close"].iloc[-1]) if "close" in df.columns else None
            indicator_results = _compute_indicators(df, normalized_indicators)

            return {
                "symbol": symbol,
                "price": current_price,
                "instrument_type": market_type,
                "source": source,
                "indicators": indicator_results,
            }

        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_indicators",
        description=(
            "Calculate technical indicators for a symbol. Available indicators: "
            "sma (Simple Moving Average), ema (Exponential Moving Average), "
            "rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), "
            "atr (Average True Range), pivot (Pivot Points)."
        ),
    )
    async def get_indicators(
        symbol: str, indicators: list[str], market: str | None = None
    ) -> dict[str, Any]:
        return await _get_indicators_impl(symbol, indicators, market)


# ---------------------------------------------------------------------------
# Public/Shared Exports
# ---------------------------------------------------------------------------

__all__ = [
    "IndicatorType",
    "DEFAULT_SMA_PERIODS",
    "DEFAULT_EMA_PERIODS",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_MACD_FAST",
    "DEFAULT_MACD_SLOW",
    "DEFAULT_MACD_SIGNAL",
    "DEFAULT_BOLLINGER_PERIOD",
    "DEFAULT_BOLLINGER_STD",
    "DEFAULT_ATR_PERIOD",
    "FIBONACCI_LEVELS",
    "_fetch_quote_crypto",
    "_fetch_quote_equity_kr",
    "_fetch_quote_equity_us",
    "_fetch_ohlcv_crypto",
    "_fetch_ohlcv_equity_kr",
    "_fetch_ohlcv_equity_us",
    "_fetch_ohlcv_crypto_paginated",
    "_fetch_ohlcv_for_indicators",
    "_fetch_ohlcv_for_volume_profile",
    "_calculate_sma",
    "_calculate_ema",
    "_calculate_rsi",
    "_calculate_macd",
    "_calculate_bollinger",
    "_calculate_atr",
    "_calculate_pivot",
    "_calculate_fibonacci",
    "_compute_indicators",
    "_format_fibonacci_source",
    "_cluster_price_levels",
    "_split_support_resistance_levels",
    "_compute_rsi_weights",
    "_compute_dca_price_levels",
    "_normalize_number",
    "_calculate_volume_profile",
    "MARKET_DATA_TOOL_NAMES",
    "_register_market_data_tools_impl",
]
