"""Technical indicator and support/resistance helpers for market data."""

from __future__ import annotations

import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.services import upbit_ohlcv_cache as upbit_ohlcv_cache_service
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient

IndicatorType = Literal[
    "sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot", "adx", "stoch_rsi", "obv"
]

# Type alias for indicator scalar values (supports string for OBV divergence)
IndicatorScalar = float | str | None

DEFAULT_SMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_EMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_RSI_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_STD = 2.0
DEFAULT_ATR_PERIOD = 14
DEFAULT_ADX_PERIOD = 14
DEFAULT_STOCH_RSI_PERIOD = 14
DEFAULT_STOCH_RSI_K_PERIOD = 3
DEFAULT_STOCH_RSI_D_PERIOD = 3
DEFAULT_OBV_SIGNAL_PERIOD = 20

FIBONACCI_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


async def _fetch_ohlcv_crypto_paginated(
    symbol: str, count: int, period: str = "day"
) -> pd.DataFrame:
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
            earliest_date = df_batch["date"].min()
            end_date = datetime.datetime.combine(
                earliest_date - datetime.timedelta(days=1),
                datetime.time(23, 59, 59),
            )

    if not all_dfs:
        return pd.DataFrame()

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
    if market_type == "crypto":
        cached = await upbit_ohlcv_cache_service.get_closed_daily_candles(
            symbol, count=count
        )
        if cached is not None:
            return cached

        fallback = await _fetch_ohlcv_crypto_paginated(
            symbol, count=count, period="day"
        )
        if fallback.empty or "date" not in fallback.columns:
            return fallback

        target_closed_date = upbit_ohlcv_cache_service.get_target_closed_date_kst()
        return (
            fallback[fallback["date"] <= target_closed_date]
            .sort_values("date")
            .reset_index(drop=True)
        )
    if market_type == "equity_kr":
        capped_count = min(count, 250)
        kis = KISClient()
        return await kis.inquire_daily_itemchartprice(
            code=symbol, market="J", n=capped_count, period="D"
        )
    capped_count = min(count, 250)
    return await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=capped_count, period="day"
    )


async def _fetch_ohlcv_for_volume_profile(
    symbol: str, market_type: str, period_days: int
) -> pd.DataFrame:
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


def _calculate_sma(
    close: pd.Series, periods: list[int] | None = None
) -> dict[str, float | None]:
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

    prev_high = float(high.iloc[-2])
    prev_low = float(low.iloc[-2])
    prev_close = float(close.iloc[-2])

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


def _calculate_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = DEFAULT_ADX_PERIOD
) -> dict[str, float | None]:
    if len(close) < period * 2:
        return {"adx": None, "plus_di": None, "minus_di": None}

    prev_close = close.shift(1)
    # Calculate up_move and down_move separately (standard ADX DM calculation)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    # Apply DM rules: +DM when up_move > down_move AND up_move > 0
    #                -DM when down_move > up_move AND down_move > 0
    # Important: use original up_move/down_move for comparison, not filtered values
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()
    smoothed_minus_dm = minus_dm.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()

    plus_di = 100 * (smoothed_plus_dm / atr.replace(0, np.nan))
    minus_di = 100 * (smoothed_minus_dm / atr.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    adx_val = adx.iloc[-1]
    plus_di_val = plus_di.iloc[-1]
    minus_di_val = minus_di.iloc[-1]

    return {
        "adx": round(float(adx_val), 2) if pd.notna(adx_val) else None,
        "plus_di": round(float(plus_di_val), 2) if pd.notna(plus_di_val) else None,
        "minus_di": round(float(minus_di_val), 2) if pd.notna(minus_di_val) else None,
    }


def _calculate_stoch_rsi(
    close: pd.Series,
    rsi_period: int = DEFAULT_STOCH_RSI_PERIOD,
    k_period: int = DEFAULT_STOCH_RSI_K_PERIOD,
    d_period: int = DEFAULT_STOCH_RSI_D_PERIOD,
) -> dict[str, float | None]:
    min_data = rsi_period + k_period + d_period
    if len(close) < min_data:
        return {"k": None, "d": None}

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(
        alpha=1 / rsi_period, min_periods=rsi_period, adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / rsi_period, min_periods=rsi_period, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    rsi_min = rsi.rolling(window=rsi_period, min_periods=1).min()
    rsi_max = rsi.rolling(window=rsi_period, min_periods=1).max()

    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    percent_k = stoch_rsi.rolling(window=k_period, min_periods=k_period).mean() * 100
    percent_d = percent_k.rolling(window=d_period, min_periods=d_period).mean()

    k_val = percent_k.iloc[-1]
    d_val = percent_d.iloc[-1]

    return {
        "k": round(float(k_val), 2) if pd.notna(k_val) else None,
        "d": round(float(d_val), 2) if pd.notna(d_val) else None,
    }


def _calculate_obv(
    close: pd.Series, volume: pd.Series, signal_period: int = DEFAULT_OBV_SIGNAL_PERIOD
) -> dict[str, IndicatorScalar]:
    if len(close) < signal_period or len(volume) < signal_period:
        return {"obv": None, "signal": None, "divergence": None}

    direction = np.where(
        close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0)
    )
    obv = (volume * direction).cumsum()
    obv_signal = obv.ewm(span=signal_period, adjust=False).mean()

    obv_val = obv.iloc[-1]
    signal_val = obv_signal.iloc[-1]

    lookback = min(10, len(close) - 1)
    if lookback < 2:
        divergence: str = "none"
    else:
        price_change = close.iloc[-1] - close.iloc[-lookback - 1]
        obv_change = obv.iloc[-1] - obv.iloc[-lookback - 1]

        if price_change < 0 and obv_change > 0:
            divergence = "bullish"
        elif price_change > 0 and obv_change < 0:
            divergence = "bearish"
        else:
            divergence = "none"

    return {
        "obv": round(float(obv_val), 2) if pd.notna(obv_val) else None,
        "signal": round(float(signal_val), 2) if pd.notna(signal_val) else None,
        "divergence": divergence,
    }


def _calculate_fibonacci(df: pd.DataFrame, current_price: float) -> dict[str, Any]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    swing_high_price = round(float(high.max()), 2)
    swing_low_price = round(float(low.min()), 2)
    swing_high_pos = int(high.values.argmax())
    swing_low_pos = int(low.values.argmin())

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

    if swing_high_pos > swing_low_pos:
        trend = "retracement_from_high"
        levels = {
            str(lvl): round(
                swing_high_price - lvl * (swing_high_price - swing_low_price), 2
            )
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        levels = {
            str(lvl): round(
                swing_low_price + lvl * (swing_high_price - swing_low_price), 2
            )
            for lvl in FIBONACCI_LEVELS
        }

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
) -> dict[str, dict[str, IndicatorScalar]]:
    results: dict[str, dict[str, IndicatorScalar]] = {}

    required = {"close"}
    if "atr" in indicators or "pivot" in indicators or "adx" in indicators:
        required |= {"high", "low"}
    if "obv" in indicators:
        required |= {"volume"}

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else None
    low = df["low"].astype(float) if "low" in df.columns else None
    volume = df["volume"].astype(float) if "volume" in df.columns else None

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
        elif indicator == "adx":
            if high is not None and low is not None:
                results["adx"] = _calculate_adx(high, low, close)
            else:
                results["adx"] = {"adx": None, "plus_di": None, "minus_di": None}
        elif indicator == "stoch_rsi":
            results["stoch_rsi"] = _calculate_stoch_rsi(close)
        elif indicator == "obv":
            if volume is not None:
                results["obv"] = _calculate_obv(close, volume)
            else:
                results["obv"] = {"obv": None, "signal": None, "divergence": None}

    return results


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
            clusters.append({"prices": [price], "sources": [source], "center": price})
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


def _compute_rsi_weights(rsi_value: float | None, splits: int) -> list[float]:
    if rsi_value is None:
        return [1.0 / splits] * splits
    if rsi_value < 30:
        raw = [splits - i for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    if rsi_value > 50:
        raw = [i + 1 for i in range(splits)]
        total = sum(raw)
        return [r / total for r in raw]
    return [1.0 / splits] * splits


def _compute_dca_price_levels(
    strategy: str,
    splits: int,
    current_price: float,
    supports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    support_prices = sorted(
        [_to_float(level.get("price")) for level in supports if level.get("price")],
        reverse=True,
    )

    if strategy == "support":
        if len(support_prices) >= splits:
            return [
                {"price": price, "source": "support"}
                for price in support_prices[:splits]
            ]
        if len(support_prices) > 0:
            support_levels: list[dict[str, Any]] = []
            start_price = current_price * 0.995
            end_price = min(support_prices)
            step = (end_price - start_price) / (splits - 1)
            used_supports: set[float] = set()
            for i in range(splits):
                price = start_price + step * i
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
        return [
            {"price": current_price * (1.0 - 0.02 * (i + 1)), "source": "synthetic"}
            for i in range(splits)
        ]

    if strategy == "equal":
        min_price = min(support_prices) if support_prices else current_price * 0.90
        start_price = current_price * 0.995
        step = (min_price - start_price) / (splits - 1)
        return [
            {"price": start_price + step * i, "source": "equal_spaced"}
            for i in range(splits)
        ]

    if strategy == "aggressive":
        first_price = current_price * 0.995
        levels: list[dict[str, Any]] = [
            {"price": first_price, "source": "aggressive_first"}
        ]
        if splits <= 1:
            return levels

        support_prices = [s["price"] for s in supports]
        end_price = min(support_prices) if support_prices else current_price * 0.98
        remaining = splits - 1
        aggressive_used_supports: set[float] = set()

        if len(support_prices) >= remaining:
            for i in range(1, splits):
                price = first_price + ((end_price - first_price) / (splits - 1)) * i
                near_support = None
                for supp in support_prices:
                    if supp in aggressive_used_supports:
                        continue
                    if abs(price - supp) / price < 0.02 and supp < price:
                        near_support = supp
                        break
                if near_support is not None:
                    price = near_support
                    aggressive_used_supports.add(near_support)
                source = "support" if near_support else "interpolated"
                levels.append({"price": price, "source": source})
        else:
            step = (end_price - first_price) / remaining
            for i in range(1, splits):
                price = first_price + step * i
                levels.append({"price": price, "source": "interpolated"})
        return levels

    raise ValueError(
        f"Invalid strategy: {strategy}. Must be 'support', 'equal', or 'aggressive'"
    )


def _normalize_number(value: float, decimals: int = 6) -> float | int:
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


def _calculate_volume_profile(
    df: pd.DataFrame,
    bins: int,
    value_area_ratio: float = 0.70,
) -> dict[str, Any]:
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

    valid_mask = low.notna() & high.notna() & volume.notna()
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


__all__ = [
    "IndicatorType",
    "IndicatorScalar",
    "DEFAULT_SMA_PERIODS",
    "DEFAULT_EMA_PERIODS",
    "DEFAULT_RSI_PERIOD",
    "DEFAULT_MACD_FAST",
    "DEFAULT_MACD_SLOW",
    "DEFAULT_MACD_SIGNAL",
    "DEFAULT_BOLLINGER_PERIOD",
    "DEFAULT_BOLLINGER_STD",
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_ADX_PERIOD",
    "DEFAULT_STOCH_RSI_PERIOD",
    "DEFAULT_STOCH_RSI_K_PERIOD",
    "DEFAULT_STOCH_RSI_D_PERIOD",
    "DEFAULT_OBV_SIGNAL_PERIOD",
    "FIBONACCI_LEVELS",
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
    "_calculate_adx",
    "_calculate_stoch_rsi",
    "_calculate_obv",
    "_calculate_fibonacci",
    "_compute_indicators",
    "_format_fibonacci_source",
    "_cluster_price_levels",
    "_split_support_resistance_levels",
    "_compute_rsi_weights",
    "_compute_dca_price_levels",
    "_normalize_number",
    "_calculate_volume_profile",
]
