"""Technical indicator and support/resistance helpers for market data."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Literal

import numpy as np
import pandas as pd

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
import app.services.kis_ohlcv_cache as kis_ohlcv_cache
from app.mcp_server.tooling.shared import (
    round_price_for_market,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.services.brokers.kis.client import KISClient
from app.services.technical_math import (
    DEFAULT_BOLLINGER_PERIOD,
    DEFAULT_BOLLINGER_STD,
    DEFAULT_RSI_PERIOD,
    FIBONACCI_LEVELS,
)
from app.services.technical_math import (
    calculate_bollinger as _calculate_bollinger,
)
from app.services.technical_math import (
    calculate_fibonacci as _calculate_fibonacci,
)
from app.services.technical_math import (
    calculate_rsi as _calculate_rsi,
)
from app.services.technical_math import (
    calculate_rsi_series as _calculate_rsi_series,
)
from app.services.technical_math import (
    calculate_volume_profile as _calculate_volume_profile,
)
from app.services.technical_math import (
    cluster_price_levels as _cluster_price_levels,
)
from app.services.technical_math import (
    collect_support_resistance_price_levels as _collect_support_resistance_price_levels,
)
from app.services.technical_math import (
    format_fibonacci_source as _format_fibonacci_source,
)
from app.services.technical_math import (
    normalize_number as _normalize_number,
)
from app.services.technical_math import (
    split_support_resistance_levels as _split_support_resistance_levels,
)

logger = logging.getLogger(__name__)

IndicatorType = Literal[
    "sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot", "adx", "stoch_rsi", "obv"
]

# Type alias for indicator scalar values (supports string for OBV divergence)
IndicatorScalar = float | str | None

DEFAULT_SMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_EMA_PERIODS = [5, 20, 60, 120, 200]
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_ATR_PERIOD = 14
DEFAULT_ADX_PERIOD = 14
DEFAULT_STOCH_RSI_PERIOD = 14
DEFAULT_STOCH_RSI_K_PERIOD = 3
DEFAULT_STOCH_RSI_D_PERIOD = 3
DEFAULT_OBV_SIGNAL_PERIOD = 20


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
    symbol: str,
    market_type: str,
    count: int = 250,
    *,
    crypto_instrument_id: int | None = None,
) -> pd.DataFrame:
    if market_type == "crypto":
        return await _cache_first_crypto(
            symbol=symbol,
            count=count,
            instrument_id=crypto_instrument_id,
        )
    if market_type == "equity_kr":
        return await _cache_first_kr(symbol=symbol, count=count)
    return await _cache_first_us(symbol=symbol, count=count)


async def _fetch_ohlcv_for_volume_profile(
    symbol: str, market_type: str, period_days: int
) -> pd.DataFrame:
    if market_type == "crypto":
        return await _fetch_ohlcv_crypto_paginated(
            symbol=symbol, count=period_days, period="day"
        )
    if market_type == "equity_kr":
        kis = KISClient()

        async def _raw_fetch_kr_daily_vp(n: int) -> pd.DataFrame:
            return await kis.inquire_daily_itemchartprice(
                code=symbol, market="J", n=n, period="D"
            )

        return await kis_ohlcv_cache.get_candles(
            symbol=symbol,
            count=period_days,
            period="day",
            raw_fetcher=_raw_fetch_kr_daily_vp,
        )
    return await yahoo_service.fetch_ohlcv(
        ticker=symbol, days=period_days, period="day"
    )


# ---------------------------------------------------------------------------
# Daily candle store helpers (cache-first read path)
# ---------------------------------------------------------------------------

# Re-exported under underscore aliases to preserve existing call sites. The
# canonical implementations live in read_service so get_ohlcv and indicators
# share one freshness rule. ROB-639.
from app.services.daily_candles.read_service import (  # noqa: E402
    cache_is_fresh_crypto as _cache_is_fresh_crypto,
)
from app.services.daily_candles.read_service import (  # noqa: E402
    cache_is_fresh_equity as _cache_is_fresh_equity,
)
from app.services.daily_candles.read_service import (  # noqa: E402
    rows_to_frame as _rows_to_frame,
)


async def _cache_first_kr(*, symbol: str, count: int) -> pd.DataFrame:
    """Read KR daily candles from DB; fall back to KIS and upsert on miss."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kis.client import KISClient as _KISClient
    from app.services.daily_candles.kis_daily_fetcher import fetch_kr_daily_unclamped
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    partition = "KRX"
    async with AsyncSessionLocal() as session:
        repo = DailyCandlesRepository(session=session)
        cached = await repo.fetch_recent(
            market=MarketKey.KR, symbol=symbol, partition=partition, count=count
        )
        if len(cached) >= count and _cache_is_fresh_equity(cached, "XKRX"):
            logger.debug(
                "daily_candles cache hit market=%s symbol=%s rows=%d",
                "kr",
                symbol,
                len(cached),
            )
            return _rows_to_frame(cached)

        try:
            kis = _KISClient()
            frame = await fetch_kr_daily_unclamped(kis=kis, code=symbol, n=count)
        except Exception:
            logger.exception("KIS fetch failed for KR symbol; returning cached rows")
            return _rows_to_frame(cached)

        from app.services.daily_candles.converters import frame_to_rows

        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source="kis"
        )
        if repo_rows:
            await repo.upsert_rows(market=MarketKey.KR, rows=repo_rows)
            await session.commit()
        return _rows_to_frame(repo_rows) if repo_rows else _rows_to_frame(cached)


async def _cache_first_us(*, symbol: str, count: int) -> pd.DataFrame:
    """Read US daily candles from DB; fall back to KIS (then Yahoo) and upsert on miss."""
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kis.client import KISClient as _KISClient
    from app.services.daily_candles.kis_daily_fetcher import fetch_us_daily_unclamped
    from app.services.daily_candles.repository import (
        DailyCandlesRepository,
        MarketKey,
    )
    from app.services.daily_candles.yahoo_us_fallback import (
        fetch_us_daily_yahoo_fallback,
    )
    from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

    async with AsyncSessionLocal() as session:
        # Resolve exchange partition; fall back to "NASD" on any error.
        try:
            partition = await get_us_exchange_by_symbol(symbol, db=session)
        except Exception:
            logger.warning("Could not resolve US exchange; defaulting to NASD")
            partition = "NASD"

        repo = DailyCandlesRepository(session=session)
        cached = await repo.fetch_recent(
            market=MarketKey.US, symbol=symbol, partition=partition, count=count
        )
        if len(cached) >= count and _cache_is_fresh_equity(cached, "XNYS"):
            logger.debug(
                "daily_candles cache hit market=%s symbol=%s rows=%d",
                "us",
                symbol,
                len(cached),
            )
            return _rows_to_frame(cached)

        try:
            kis = _KISClient()
            frame = await fetch_us_daily_unclamped(
                kis=kis, symbol=symbol, exchange_code=partition, n=count
            )
        except Exception:
            logger.exception("KIS fetch failed for US symbol; returning cached rows")
            return _rows_to_frame(cached)

        from app.services.daily_candles.converters import frame_to_rows

        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source="kis"
        )

        if not repo_rows:
            # KIS returned empty — try Yahoo fallback.
            logger.warning("KIS returned no rows for US symbol; trying Yahoo fallback")
            try:
                yahoo_rows = await fetch_us_daily_yahoo_fallback(symbol=symbol, n=count)
            except Exception:
                logger.exception("Yahoo fallback failed for US symbol")
                yahoo_rows = []

            if yahoo_rows:
                from app.services.daily_candles.repository import DailyCandleRow

                repo_rows = [
                    DailyCandleRow(
                        time_utc=r.time_utc,
                        symbol=r.symbol,
                        partition=partition,
                        open=r.open,
                        high=r.high,
                        low=r.low,
                        close=r.close,
                        adj_close=r.adj_close,
                        volume=r.volume,
                        value=r.value,
                        source="yahoo_fallback",
                    )
                    for r in yahoo_rows
                ]

        if repo_rows:
            await repo.upsert_rows(market=MarketKey.US, rows=repo_rows)
            await session.commit()
        return _rows_to_frame(repo_rows) if repo_rows else _rows_to_frame(cached)


async def _cache_first_crypto(
    *, symbol: str, count: int, instrument_id: int | None = None
) -> pd.DataFrame:
    """Read crypto daily candles from DB; fall back to Upbit and upsert on miss.

    When ``instrument_id`` is supplied, the read/write paths skip the
    per-symbol ``crypto_instruments`` lookup that drove the holdings N+1.
    All non-holdings callers continue to omit the new keyword and retain the
    legacy one-SELECT-per-symbol path.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.daily_candles.crypto_identity import (
        upbit_daily_candle_partition,
    )
    from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey

    partition = upbit_daily_candle_partition(symbol)

    async with AsyncSessionLocal() as session:
        repo = DailyCandlesRepository(session=session)
        if instrument_id is not None:
            cached = await repo.fetch_recent_crypto_by_instrument_id(
                instrument_id=instrument_id,
                symbol=symbol,
                partition=partition,
                count=count,
            )
        else:
            cached = await repo.fetch_recent(
                market=MarketKey.CRYPTO,
                symbol=symbol,
                partition=partition,
                count=count,
            )
        if len(cached) >= count and _cache_is_fresh_crypto(cached):
            logger.debug(
                "daily_candles cache hit market=%s symbol=%s rows=%d",
                "crypto",
                symbol,
                len(cached),
            )
            return _rows_to_frame(cached)

        try:
            frame = await upbit_service.fetch_ohlcv(
                market=symbol, days=count, period="day"
            )
        except Exception:
            logger.exception(
                "Upbit fetch failed for crypto symbol; returning cached rows"
            )
            return _rows_to_frame(cached)

        from app.services.daily_candles.converters import frame_to_rows

        repo_rows = frame_to_rows(
            frame, symbol=symbol, partition=partition, source="upbit"
        )
        if repo_rows:
            if instrument_id is not None:
                await repo.upsert_crypto_rows_by_instrument_id(
                    instrument_id=instrument_id, rows=repo_rows
                )
            else:
                await repo.upsert_rows(market=MarketKey.CRYPTO, rows=repo_rows)
            await session.commit()
        return _rows_to_frame(repo_rows) if repo_rows else _rows_to_frame(cached)


def _normalize_crypto_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return ""
    if "-" not in normalized:
        return f"KRW-{normalized}"
    return normalized


def _apply_realtime_price_to_last_close(
    close: pd.Series,
    realtime_price: float | None,
) -> pd.Series:
    if realtime_price is None or close.empty:
        return close
    adjusted = close.copy()
    adjusted.iloc[-1] = float(realtime_price)
    return adjusted


def _compute_crypto_realtime_rsi_from_frame(
    frame: pd.DataFrame,
    realtime_price: float | None,
    period: int = DEFAULT_RSI_PERIOD,
) -> float | None:
    if frame.empty or "close" not in frame.columns:
        return None

    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(close) < period + 1:
        return None

    close = close.astype(float)
    realtime_close = _apply_realtime_price_to_last_close(close, realtime_price)
    rsi_value = _calculate_rsi(realtime_close, period).get(str(period))
    return _to_optional_float(rsi_value)


async def compute_crypto_realtime_rsi_map(
    symbols: list[str],
    count: int = 200,
    use_ticker_cache: bool = True,
) -> dict[str, float | None]:
    normalized_symbols: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = _normalize_crypto_symbol(symbol)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_symbols.append(normalized)

    if not normalized_symbols:
        return {}

    ticker_prices: dict[str, float] = {}
    try:
        ticker_prices = await upbit_service.fetch_multiple_current_prices(
            normalized_symbols,
            use_cache=use_ticker_cache,
        )
    except Exception:
        ticker_prices = {}

    async def _compute_symbol_rsi(symbol: str) -> tuple[str, float | None]:
        try:
            df = await _fetch_ohlcv_for_indicators(symbol, "crypto", count=count)
        except Exception:
            return symbol, None

        rsi_value = _compute_crypto_realtime_rsi_from_frame(
            df,
            _to_optional_float(ticker_prices.get(symbol)),
            period=DEFAULT_RSI_PERIOD,
        )
        return symbol, rsi_value

    results = await asyncio.gather(
        *[_compute_symbol_rsi(symbol) for symbol in normalized_symbols],
        return_exceptions=False,
    )
    return dict(results)


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
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    market_type: str | None = None,
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

    def _price(level: float) -> float:
        return round_price_for_market(level, market_type)

    return {
        "p": _price(p),
        "r1": _price(r1),
        "r2": _price(r2),
        "r3": _price(r3),
        "s1": _price(s1),
        "s2": _price(s2),
        "s3": _price(s3),
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


def _compute_indicators(
    df: pd.DataFrame,
    indicators: list[IndicatorType],
    market_type: str | None = None,
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
                results["pivot"] = _calculate_pivot(high, low, close, market_type)
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
    "_cache_is_fresh_equity",
    "_cache_is_fresh_crypto",
    "_rows_to_frame",
    "_cache_first_kr",
    "_cache_first_us",
    "_cache_first_crypto",
    "_normalize_crypto_symbol",
    "_apply_realtime_price_to_last_close",
    "_compute_crypto_realtime_rsi_from_frame",
    "compute_crypto_realtime_rsi_map",
    "_calculate_sma",
    "_calculate_ema",
    "_calculate_rsi",
    "_calculate_rsi_series",
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
    "_collect_support_resistance_price_levels",
    "_compute_rsi_weights",
    "_normalize_number",
    "_calculate_volume_profile",
]
