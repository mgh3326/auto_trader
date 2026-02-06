from __future__ import annotations

import asyncio
import datetime
import json
from typing import TYPE_CHECKING, Any, Literal

import finnhub
import httpx
import numpy as np
import pandas as pd
import yfinance as yf

if TYPE_CHECKING:
    from fastmcp import FastMCP

from app.core.config import settings
from app.services import naver_finance
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient
from data.coins_info import get_or_refresh_maps

# 마스터 데이터 (lazy loading)
from data.stocks_info import (
    get_kosdaq_name_to_code,
    get_kospi_name_to_code,
    get_us_stocks_data,
)


def _is_korean_equity_code(symbol: str) -> bool:
    """Check if symbol is a valid Korean equity code (6 alphanumeric characters).

    Korean stock codes are 6 characters:
    - Regular stocks: 6 digits (e.g., 005930)
    - ETF/ETN: 6 alphanumeric (e.g., 0123G0, 0117V0)
    """
    s = symbol.strip().upper()
    return len(s) == 6 and s.isalnum()


def _is_crypto_market(symbol: str) -> bool:
    s = symbol.strip().upper()
    return s.startswith("KRW-") or s.startswith("USDT-")


def _is_us_equity_symbol(symbol: str) -> bool:
    # Simple heuristic: has letters and no dash-prefix like KRW-
    s = symbol.strip().upper()
    return (not _is_crypto_market(s)) and any(c.isalpha() for c in s)


def _normalize_market(market: str | None) -> str | None:
    if not market:
        return None
    normalized = market.strip().lower()
    if not normalized:
        return None
    mapping = {
        "crypto": "crypto",
        "upbit": "crypto",
        "krw": "crypto",
        "usdt": "crypto",
        "kr": "equity_kr",
        "krx": "equity_kr",
        "korea": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "kis": "equity_kr",
        "equity_kr": "equity_kr",
        "us": "equity_us",
        "usa": "equity_us",
        "nyse": "equity_us",
        "nasdaq": "equity_us",
        "yahoo": "equity_us",
        "equity_us": "equity_us",
    }
    return mapping.get(normalized)


def _resolve_market_type(symbol: str, market: str | None) -> tuple[str, str]:
    """Resolve market type and validate symbol.

    Returns (market_type, normalized_symbol) or raises ValueError.
    """
    market_type = _normalize_market(market)

    # Explicit market specified - validate symbol format
    if market_type == "crypto":
        symbol = symbol.upper()
        if not _is_crypto_market(symbol):
            raise ValueError("crypto symbols must include KRW-/USDT- prefix")
        return "crypto", symbol

    if market_type == "equity_kr":
        if not _is_korean_equity_code(symbol):
            raise ValueError("korean equity symbols must be 6 alphanumeric characters")
        return "equity_kr", symbol

    if market_type == "equity_us":
        if _is_crypto_market(symbol):
            raise ValueError("us equity symbols must not include KRW-/USDT- prefix")
        return "equity_us", symbol

    # Auto-detect from symbol format
    if _is_crypto_market(symbol):
        return "crypto", symbol.upper()

    if _is_korean_equity_code(symbol):
        return "equity_kr", symbol

    if _is_us_equity_symbol(symbol):
        return "equity_us", symbol

    raise ValueError("Unsupported symbol format")


def _error_payload(
    *,
    source: str,
    message: str,
    symbol: str | None = None,
    instrument_type: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message, "source": source}
    if symbol is not None:
        payload["symbol"] = symbol
    if instrument_type is not None:
        payload["instrument_type"] = instrument_type
    if query is not None:
        payload["query"] = query
    return payload


def _normalize_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _normalize_value(value) for key, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


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


async def _fetch_ohlcv_crypto(
    symbol: str, count: int, period: str, end_date: datetime.datetime | None
) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_count = min(count, 200)
    df = await upbit_service.fetch_ohlcv(
        market=symbol, days=capped_count, period=period, end_date=end_date
    )
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


# ---------------------------------------------------------------------------
# Technical Indicator Calculations
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
            else:
                results["pivot"] = {
                    "p": None,
                    "r1": None,
                    "r2": None,
                    "r3": None,
                    "s1": None,
                    "s2": None,
                    "s3": None,
                }

    return results


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


# ---------------------------------------------------------------------------
# Finnhub API Helpers
# ---------------------------------------------------------------------------


def _get_finnhub_client() -> finnhub.Client:
    """Get Finnhub client with API key from settings."""
    api_key = settings.finnhub_api_key
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def _fetch_news_finnhub(symbol: str, market: str, limit: int) -> dict[str, Any]:
    """Fetch news from Finnhub API.

    Args:
        symbol: Stock symbol (e.g., "AAPL") or crypto symbol (e.g., "BINANCE:BTCUSDT")
        market: Market type - "us" or "crypto"
        limit: Maximum number of news items to return

    Returns:
        Dictionary with news data
    """
    client = _get_finnhub_client()

    # Calculate date range (last 7 days for company news)
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    def fetch_sync() -> list[dict[str, Any]]:
        if market == "crypto":
            # For crypto, use general news with crypto category
            news = client.general_news("crypto", min_id=0)
        else:
            # For US stocks, use company news
            news = client.company_news(
                symbol.upper(),
                _from=from_date.strftime("%Y-%m-%d"),
                to=to_date.strftime("%Y-%m-%d"),
            )
        return news[:limit] if news else []

    news_items = await asyncio.to_thread(fetch_sync)

    # Transform to consistent format
    result_items = []
    for item in news_items:
        result_items.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "datetime": datetime.datetime.fromtimestamp(
                    item.get("datetime", 0)
                ).isoformat()
                if item.get("datetime")
                else None,
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "sentiment": item.get("sentiment"),  # May be None
                "related": item.get("related", ""),
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "source": "finnhub",
        "count": len(result_items),
        "news": result_items,
    }


async def _fetch_company_profile_finnhub(symbol: str) -> dict[str, Any]:
    """Fetch company profile from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")

    Returns:
        Dictionary with company profile data
    """
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.company_profile2(symbol=symbol.upper())

    profile = await asyncio.to_thread(fetch_sync)

    if not profile:
        raise ValueError(f"Company profile not found for symbol '{symbol}'")

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "name": profile.get("name", ""),
        "ticker": profile.get("ticker", ""),
        "country": profile.get("country", ""),
        "currency": profile.get("currency", ""),
        "exchange": profile.get("exchange", ""),
        "ipo_date": profile.get("ipo", ""),
        "market_cap": profile.get("marketCapitalization"),
        "shares_outstanding": profile.get("shareOutstanding"),
        "sector": profile.get("finnhubIndustry", ""),
        "website": profile.get("weburl", ""),
        "logo": profile.get("logo", ""),
        "phone": profile.get("phone", ""),
    }


async def _fetch_financials_finnhub(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    """Fetch financial statements from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        statement: Statement type - "income", "balance", or "cashflow"
        freq: Frequency - "annual" or "quarterly"

    Returns:
        Dictionary with financial data
    """
    client = _get_finnhub_client()

    # Map statement types to Finnhub format
    statement_map = {
        "income": "ic",
        "balance": "bs",
        "cashflow": "cf",
    }
    finnhub_statement = statement_map.get(statement)
    if not finnhub_statement:
        raise ValueError(
            f"Invalid statement type '{statement}'. Use: income, balance, cashflow"
        )

    def fetch_sync() -> dict[str, Any]:
        return client.financials_reported(
            symbol=symbol.upper(),
            freq=freq,
        )

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        raise ValueError(f"Financial data not found for symbol '{symbol}'")

    # Extract relevant financial data
    reports = []
    for report in result.get("data", [])[:4]:  # Last 4 reports
        report_data = report.get("report", {})
        statement_data = report_data.get(finnhub_statement, [])

        # Convert list of dicts to a single dict
        financials = {}
        for item in statement_data:
            label = item.get("label", item.get("concept", ""))
            value = item.get("value")
            if label and value is not None:
                financials[label] = value

        reports.append(
            {
                "year": report.get("year"),
                "quarter": report.get("quarter"),
                "filed_date": report.get("filedDate"),
                "period_start": report.get("startDate"),
                "period_end": report.get("endDate"),
                "data": financials,
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "statement": statement,
        "freq": freq,
        "reports": reports,
    }


async def _fetch_insider_transactions_finnhub(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch insider transactions from Finnhub API.

    Args:
        symbol: US stock symbol (e.g., "AAPL")
        limit: Maximum number of transactions to return

    Returns:
        Dictionary with insider transaction data
    """
    client = _get_finnhub_client()

    def fetch_sync() -> dict[str, Any]:
        return client.stock_insider_transactions(symbol=symbol.upper())

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("data"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "count": 0,
            "transactions": [],
        }

    transactions = []
    for txn in result.get("data", [])[:limit]:
        # Transaction codes: P=Purchase, S=Sale, A=Grant, D=Sale to issuer, etc.
        txn_code = txn.get("transactionCode", "")
        txn_type_map = {
            "P": "Purchase",
            "S": "Sale",
            "A": "Grant/Award",
            "D": "Sale to Issuer",
            "F": "Tax Payment",
            "M": "Option Exercise",
            "G": "Gift",
            "C": "Conversion",
            "J": "Other",
        }
        transactions.append(
            {
                "name": txn.get("name", ""),
                "transaction_type": txn_type_map.get(txn_code, txn_code),
                "transaction_code": txn_code,
                "shares": txn.get("share"),
                "change": txn.get("change"),  # Net change in shares
                "price": txn.get("transactionPrice"),
                "date": txn.get("transactionDate"),
                "filing_date": txn.get("filingDate"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "count": len(transactions),
        "transactions": transactions,
    }


async def _fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Fetch earnings calendar from Finnhub API.

    Args:
        symbol: US stock symbol (optional, e.g., "AAPL")
        from_date: Start date in ISO format (optional)
        to_date: End date in ISO format (optional)

    Returns:
        Dictionary with earnings calendar data
    """
    client = _get_finnhub_client()

    # Default to next 30 days if no dates provided
    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    def fetch_sync() -> dict[str, Any]:
        # Finnhub API accepts empty string for symbol to get all earnings
        return client.earnings_calendar(
            symbol=symbol.upper() if symbol else "",
            _from=from_date,
            to=to_date,
        )

    result = await asyncio.to_thread(fetch_sync)

    if not result or not result.get("earningsCalendar"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": from_date,
            "to_date": to_date,
            "count": 0,
            "earnings": [],
        }

    earnings = []
    for item in result.get("earningsCalendar", []):
        earnings.append(
            {
                "symbol": item.get("symbol", ""),
                "date": item.get("date"),
                "hour": item.get(
                    "hour", ""
                ),  # "bmo" (before market open), "amc" (after market close)
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "from_date": from_date,
        "to_date": to_date,
        "count": len(earnings),
        "earnings": earnings,
    }


# ---------------------------------------------------------------------------
# Naver Finance Helpers (Korean Stocks)
# ---------------------------------------------------------------------------


async def _fetch_news_naver(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch news from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        limit: Maximum number of news items to return

    Returns:
        Dictionary with news data
    """
    news_items = await naver_finance.fetch_news(symbol, limit=limit)

    return {
        "symbol": symbol,
        "market": "kr",
        "source": "naver",
        "count": len(news_items),
        "news": news_items,
    }


async def _fetch_company_profile_naver(symbol: str) -> dict[str, Any]:
    """Fetch company profile from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")

    Returns:
        Dictionary with company profile data
    """
    profile = await naver_finance.fetch_company_profile(symbol)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **profile,
    }


async def _fetch_financials_naver(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    """Fetch financial statements from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        statement: Statement type - "income", "balance", or "cashflow"
        freq: Frequency - "annual" or "quarterly"

    Returns:
        Dictionary with financial statement data
    """
    financials = await naver_finance.fetch_financials(symbol, statement, freq)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **financials,
    }


async def _fetch_investor_trends_naver(symbol: str, days: int) -> dict[str, Any]:
    """Fetch investor trends from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        days: Number of days of data to fetch

    Returns:
        Dictionary with investor trend data
    """
    trends = await naver_finance.fetch_investor_trends(symbol, days=days)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **trends,
    }


async def _fetch_investment_opinions_naver(symbol: str, limit: int) -> dict[str, Any]:
    """Fetch investment opinions from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")
        limit: Maximum number of opinions to return

    Returns:
        Dictionary with investment opinion data
    """
    opinions = await naver_finance.fetch_investment_opinions(symbol, limit=limit)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **opinions,
    }


async def _fetch_investment_opinions_yfinance(
    symbol: str, limit: int
) -> dict[str, Any]:
    """Fetch analyst opinions from yfinance for US stocks.

    Uses Ticker.analyst_price_targets for consensus targets
    and Ticker.upgrades_downgrades for individual firm recommendations.

    Args:
        symbol: US stock ticker (e.g., "AAPL")
        limit: Maximum number of recommendations to return

    Returns:
        Dictionary with analyst opinion data
    """
    loop = asyncio.get_running_loop()
    ticker = yf.Ticker(symbol)

    def _collect() -> tuple[dict | None, Any, dict | None]:
        targets = None
        try:
            targets = ticker.analyst_price_targets
        except Exception:
            pass

        ud = None
        try:
            ud = ticker.upgrades_downgrades
        except Exception:
            pass

        info = None
        try:
            info = ticker.info
        except Exception:
            pass
        return targets, ud, info

    targets, ud, info = await loop.run_in_executor(None, _collect)

    current_price = (info or {}).get("currentPrice")

    # --- price targets ---
    avg_target: float | None = None
    max_target: float | None = None
    min_target: float | None = None
    if isinstance(targets, dict):
        avg_target = targets.get("mean") or targets.get("median")
        max_target = targets.get("high")
        min_target = targets.get("low")
        if current_price is None:
            current_price = targets.get("current")

    upside: float | None = None
    if current_price and avg_target:
        upside = round((avg_target - current_price) / current_price * 100, 2)

    # --- recent recommendations ---
    recommendations: list[dict[str, Any]] = []
    if ud is not None and not ud.empty:
        df = ud.head(limit).reset_index()
        for _, row in df.iterrows():
            rec: dict[str, Any] = {
                "firm": row.get("Firm"),
                "rating": row.get("ToGrade"),
                "date": (
                    row["GradeDate"].strftime("%Y-%m-%d")
                    if hasattr(row.get("GradeDate", None), "strftime")
                    else str(row.get("GradeDate", ""))[:10]
                ),
            }
            pt = row.get("currentPriceTarget")
            if pt and pt > 0:
                rec["target_price"] = float(pt)
            recommendations.append(rec)

    return {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "current_price": current_price,
        "avg_target_price": avg_target,
        "max_target_price": max_target,
        "min_target_price": min_target,
        "upside_potential": upside,
        "count": len(recommendations),
        "recommendations": recommendations,
    }


async def _fetch_valuation_naver(symbol: str) -> dict[str, Any]:
    """Fetch valuation metrics from Naver Finance for Korean stocks.

    Args:
        symbol: Korean stock code (6 digits, e.g., "005930")

    Returns:
        Dictionary with valuation metrics (PER, PBR, ROE, dividend_yield,
        52-week high/low, current price, current_position_52w)
    """
    valuation = await naver_finance.fetch_valuation(symbol)

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **valuation,
    }


async def _fetch_valuation_yfinance(symbol: str) -> dict[str, Any]:
    """Fetch valuation metrics from yfinance for US stocks.

    Args:
        symbol: US stock ticker (e.g., "AAPL", "MSFT")

    Returns:
        Dictionary with valuation metrics (PER, PBR, ROE, dividend_yield,
        52-week high/low, current price, current_position_52w)
    """
    loop = asyncio.get_running_loop()
    ticker = yf.Ticker(symbol)
    info: dict[str, Any] = await loop.run_in_executor(None, lambda: ticker.info)

    current_price = info.get("currentPrice")
    high_52w = info.get("fiftyTwoWeekHigh")
    low_52w = info.get("fiftyTwoWeekLow")

    # Calculate 52-week position
    current_position_52w = None
    if current_price is not None and high_52w is not None and low_52w is not None:
        if high_52w > low_52w:
            current_position_52w = round((current_price - low_52w) / (high_52w - low_52w), 2)

    # ROE is returned as a decimal (e.g. 1.47 = 147%), convert to percentage
    roe_raw = info.get("returnOnEquity")
    roe = round(roe_raw * 100, 2) if roe_raw is not None else None

    return {
        "instrument_type": "equity_us",
        "source": "yfinance",
        "symbol": symbol.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "current_price": current_price,
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "roe": roe,
        "dividend_yield": info.get("dividendYield"),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "current_position_52w": current_position_52w,
    }


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """마스터 데이터에서 종목 검색 (KRX, US, Crypto)

    Args:
        query: 검색어 (심볼 또는 이름)
        limit: 최대 결과 개수
        instrument_type: 필터링할 상품 유형 (equity_kr, equity_us, crypto, None=전체)
    """
    results: list[dict[str, Any]] = []
    query_lower = query.lower()
    query_upper = query.upper()

    # 1. KRX (KOSPI + KOSDAQ) 검색
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

    # 2. US Stocks 검색
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

    # 3. Crypto 검색
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
            pass  # crypto 데이터 로드 실패 시 무시

    return results


DEFAULT_KIMCHI_SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "AVAX", "DOT"]

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

# exchangerate-api.com (free, no key required)
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"


async def _fetch_exchange_rate_usd_krw() -> float:
    """Fetch current USD/KRW exchange rate."""
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(EXCHANGE_RATE_URL)
        r.raise_for_status()
        data = r.json()
        rate = data["rates"]["KRW"]
        return float(rate)


async def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch USDT prices from Binance for given symbols.

    Returns dict like {"BTC": 102000.5, "ETH": 3050.2}.
    """
    pairs = [f"{s}USDT" for s in symbols]
    async with httpx.AsyncClient(timeout=10) as cli:
        # Binance expects compact JSON without spaces for the symbols param
        symbols_json = json.dumps(pairs, separators=(",", ":"))
        r = await cli.get(
            BINANCE_TICKER_URL,
            params={"symbols": symbols_json},
        )
        r.raise_for_status()
        data = r.json()

    result: dict[str, float] = {}
    for item in data:
        pair: str = item["symbol"]  # e.g. "BTCUSDT"
        if pair.endswith("USDT"):
            sym = pair[: -len("USDT")]
            result[sym] = float(item["price"])
    return result


async def _fetch_kimchi_premium(symbols: list[str]) -> dict[str, Any]:
    """Calculate kimchi premium for given crypto symbols.

    Compares Upbit KRW prices with Binance USDT prices * USD/KRW rate.
    """
    upbit_markets = [f"KRW-{s}" for s in symbols]

    # Fetch all three data sources concurrently
    upbit_prices, binance_prices, exchange_rate = await asyncio.gather(
        upbit_service.fetch_multiple_current_prices(upbit_markets),
        _fetch_binance_prices(symbols),
        _fetch_exchange_rate_usd_krw(),
    )

    data: list[dict[str, Any]] = []
    for sym in symbols:
        upbit_key = f"KRW-{sym}"
        upbit_krw = upbit_prices.get(upbit_key)
        binance_usdt = binance_prices.get(sym)

        if upbit_krw is None or binance_usdt is None:
            continue

        binance_krw = binance_usdt * exchange_rate
        premium_pct = round((upbit_krw - binance_krw) / binance_krw * 100, 2)

        data.append(
            {
                "symbol": sym,
                "upbit_krw": upbit_krw,
                "binance_usdt": binance_usdt,
                "binance_krw": round(binance_krw, 0),
                "premium_pct": premium_pct,
            }
        )

    now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "instrument_type": "crypto",
        "source": "upbit+binance",
        "timestamp": now,
        "exchange_rate": exchange_rate,
        "count": len(data),
        "data": data,
    }


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol",
        description="Search symbols by query (symbol or name). Use market to filter: kr/kospi/kosdaq (Korean stocks), us/nasdaq/nyse (US stocks), crypto/upbit (cryptocurrencies).",
    )
    async def search_symbol(
        query: str, limit: int = 20, market: str | None = None
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        # market 정규화 (get_quote, get_ohlcv와 동일한 로직)
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
    async def get_quote(symbol: str, market: str | None = None) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_quote_crypto(symbol)
            elif market_type == "equity_kr":
                return await _fetch_quote_equity_kr(symbol)
            else:  # equity_us
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
        description="Get OHLCV candles for a symbol. Supports daily/weekly/monthly periods and date-based pagination.",
    )
    async def get_ohlcv(
        symbol: str,
        count: int = 100,
        period: str = "day",
        end_date: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get OHLCV candles.

        Args:
            symbol: Symbol to query (e.g., "005930", "AAPL", "KRW-BTC")
            count: Number of candles to return (max 200 for crypto/kr, 100 for us)
            period: Candle period - "day", "week", or "month"
            end_date: End date for pagination (ISO format: "2024-01-15"). None = latest
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)
        """
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
            except ValueError:
                raise ValueError("end_date must be ISO format (e.g., '2024-01-15')")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_ohlcv_crypto(symbol, count, period, parsed_end_date)
            elif market_type == "equity_kr":
                return await _fetch_ohlcv_equity_kr(
                    symbol, count, period, parsed_end_date
                )
            else:  # equity_us
                return await _fetch_ohlcv_equity_us(
                    symbol, count, period, parsed_end_date
                )
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_indicators",
        description="Calculate technical indicators for a symbol. Available indicators: sma (Simple Moving Average), ema (Exponential Moving Average), rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), atr (Average True Range), pivot (Pivot Points).",
    )
    async def get_indicators(
        symbol: str,
        indicators: list[str],
        market: str | None = None,
    ) -> dict[str, Any]:
        """Calculate technical indicators for a symbol.

        Args:
            symbol: Symbol to query (e.g., "005930", "AAPL", "KRW-BTC")
            indicators: List of indicators to calculate. Options:
                - "sma": Simple Moving Average (periods: 5, 20, 60, 120, 200)
                - "ema": Exponential Moving Average (periods: 5, 20, 60, 120, 200)
                - "rsi": RSI (period: 14)
                - "macd": MACD (fast: 12, slow: 26, signal: 9)
                - "bollinger": Bollinger Bands (period: 20, std: 2)
                - "atr": Average True Range (period: 14)
                - "pivot": Pivot Points (classic formula)
            market: Market hint - kr/us/crypto (optional, auto-detected from symbol)

        Returns:
            Dictionary with symbol, current price, instrument_type, source, and indicators
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not indicators:
            raise ValueError("indicators list is required and cannot be empty")

        # Validate indicator names
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
            # Fetch enough data for long-term indicators (200-day SMA needs 200+ candles)
            df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")

            # Get current price from the latest row
            current_price = (
                float(df["close"].iloc[-1]) if "close" in df.columns else None
            )

            # Compute requested indicators
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

    # ---------------------------------------------------------------------------
    # Finnhub Tools (News & Fundamentals)
    # ---------------------------------------------------------------------------

    @mcp.tool(
        name="get_news",
        description="Get recent news for a stock or cryptocurrency. Supports US stocks (Finnhub), Korean stocks (Naver Finance), and crypto (Finnhub).",
    )
    async def get_news(
        symbol: str,
        market: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get recent news for a symbol.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean) or "crypto"
            market: Market type - "us", "kr", or "crypto" (auto-detected if not specified)
            limit: Maximum number of news items (default: 10, max: 50)

        Returns:
            Dictionary with news items including title, source, datetime, url
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            elif _is_crypto_market(symbol):
                market = "crypto"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in ("crypto", "upbit", "krw", "usdt"):
            normalized_market = "crypto"
        elif normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us', 'kr', or 'crypto'")

        capped_limit = min(max(limit, 1), 50)

        try:
            if normalized_market == "kr":
                return await _fetch_news_naver(symbol, capped_limit)
            else:
                return await _fetch_news_finnhub(
                    symbol, normalized_market, capped_limit
                )
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = {
                "kr": "equity_kr",
                "us": "equity_us",
                "crypto": "crypto",
            }.get(normalized_market, "equity_us")
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_company_profile",
        description="Get company profile for a US or Korean stock. Returns name, sector, industry, market cap, and financial ratios.",
    )
    async def get_company_profile(
        symbol: str, market: str | None = None
    ) -> dict[str, Any]:
        """Get company profile for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with company profile including name, sector, market_cap
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        # Crypto not supported
        if _is_crypto_market(symbol):
            raise ValueError("Company profile is not available for cryptocurrencies")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_company_profile_naver(symbol)
            else:
                return await _fetch_company_profile_finnhub(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_financials",
        description="Get financial statements for a US or Korean stock. Supports income statement, balance sheet, and cash flow.",
    )
    async def get_financials(
        symbol: str,
        statement: str = "income",
        freq: str = "annual",
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get financial statements for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            statement: Statement type - "income", "balance", or "cashflow" (default: "income")
            freq: Frequency - "annual" or "quarterly" (default: "annual")
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with financial statement data
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        statement = (statement or "income").strip().lower()
        if statement not in ("income", "balance", "cashflow"):
            raise ValueError("statement must be 'income', 'balance', or 'cashflow'")

        freq = (freq or "annual").strip().lower()
        if freq not in ("annual", "quarterly"):
            raise ValueError("freq must be 'annual' or 'quarterly'")

        # Crypto not supported
        if _is_crypto_market(symbol):
            raise ValueError(
                "Financial statements are not available for cryptocurrencies"
            )

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        # Normalize market type
        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_financials_naver(symbol, statement, freq)
            else:
                return await _fetch_financials_finnhub(symbol, statement, freq)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_insider_transactions",
        description="Get insider transactions for a US stock. Returns name, transaction type, shares, price, date. US stocks only.",
    )
    async def get_insider_transactions(
        symbol: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Get insider transactions for a US stock.

        Args:
            symbol: US stock symbol (e.g., "AAPL")
            limit: Maximum number of transactions (default: 20, max: 100)

        Returns:
            Dictionary with insider transaction data
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        capped_limit = min(max(limit, 1), 100)

        # Validate this is a US equity symbol
        if _is_crypto_market(symbol):
            raise ValueError("Insider transactions are only available for US stocks")
        if _is_korean_equity_code(symbol):
            raise ValueError("Insider transactions are only available for US stocks")

        try:
            return await _fetch_insider_transactions_finnhub(symbol, capped_limit)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

    @mcp.tool(
        name="get_earnings_calendar",
        description="Get earnings calendar for a US stock or date range. Returns earnings dates, EPS estimates and actuals. US stocks only.",
    )
    async def get_earnings_calendar(
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get earnings calendar.

        Args:
            symbol: US stock symbol (optional, e.g., "AAPL"). If not provided, returns all earnings in date range.
            from_date: Start date in ISO format (optional, default: today)
            to_date: End date in ISO format (optional, default: 30 days from now)

        Returns:
            Dictionary with earnings calendar including dates, EPS estimates and actuals
        """
        symbol = (symbol or "").strip() if symbol else None

        # Validate symbol if provided
        if symbol:
            if _is_crypto_market(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")
            if _is_korean_equity_code(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")

        # Validate date formats if provided
        if from_date:
            try:
                datetime.date.fromisoformat(from_date)
            except ValueError:
                raise ValueError("from_date must be ISO format (e.g., '2024-01-15')")

        if to_date:
            try:
                datetime.date.fromisoformat(to_date)
            except ValueError:
                raise ValueError("to_date must be ISO format (e.g., '2024-01-15')")

        try:
            return await _fetch_earnings_calendar_finnhub(symbol, from_date, to_date)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

    # ---------------------------------------------------------------------------
    # Naver Finance Tools (Korean Stocks Only)
    # ---------------------------------------------------------------------------

    @mcp.tool(
        name="get_investor_trends",
        description="Get foreign and institutional investor trading trends for a Korean stock. Returns daily net buy/sell data. Korean stocks only.",
    )
    async def get_investor_trends(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        """Get investor trading trends for a Korean stock.

        Args:
            symbol: Korean stock code (6 digits, e.g., "005930")
            days: Number of days of data (default: 20, max: 60)

        Returns:
            Daily investor flow data including foreign, institutional net trades
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Investor trends are only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await _fetch_investor_trends_naver(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="naver",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

    @mcp.tool(
        name="get_investment_opinions",
        description="Get securities firm investment opinions and target prices for a US or Korean stock. Returns analyst ratings, price targets, and upside potential.",
    )
    async def get_investment_opinions(
        symbol: str,
        limit: int = 10,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Get investment opinions for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            limit: Maximum number of opinions (default: 10, max: 30)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Investment opinions including firm name, target price, rating, date
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError(
                "Investment opinions are not available for cryptocurrencies"
            )

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        capped_limit = min(max(limit, 1), 30)

        try:
            if normalized_market == "kr":
                return await _fetch_investment_opinions_naver(symbol, capped_limit)
            else:
                return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_valuation",
        description="Get valuation metrics for a US or Korean stock. Returns PER, PBR, ROE, dividend yield, 52-week high/low, current price, and position within 52-week range.",
    )
    async def get_valuation(
        symbol: str, market: str | None = None
    ) -> dict[str, Any]:
        """Get valuation metrics for a stock.

        Args:
            symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean)
            market: Market type - "us" or "kr" (auto-detected if not specified)

        Returns:
            Dictionary with valuation metrics:
            - symbol: Stock code
            - name: Company name
            - current_price: Current stock price
            - per: Price-to-Earnings Ratio
            - pbr: Price-to-Book Ratio
            - roe: Return on Equity (%)
            - dividend_yield: Dividend yield (as decimal, e.g., 0.02 for 2%)
            - high_52w: 52-week high price
            - low_52w: 52-week low price
            - current_position_52w: Position within 52-week range (0=low, 1=high)
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Valuation metrics are not available for cryptocurrencies")

        # Auto-detect market if not specified
        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr", "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_valuation_naver(symbol)
            else:
                return await _fetch_valuation_yfinance(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_short_interest",
        description="Get short selling data for a Korean stock. Returns daily short selling volume, amount, ratio, and balance. Korean stocks only.",
    )
    async def get_short_interest(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        """Get short selling data for a Korean stock.

        Args:
            symbol: Korean stock code (6 digits, e.g., "005930" for Samsung Electronics)
            days: Number of days of data to fetch (default: 20, max: 60)

        Returns:
            Dictionary with short selling data:
            - symbol: Stock code
            - name: Company name
            - short_data: List of daily short selling data
                - date: Trading date (ISO format)
                - short_volume: Short selling volume (shares, if available)
                - short_amount: Short selling amount (KRW)
                - short_ratio: Short selling ratio (%)
                - total_volume: Total trading volume (shares, if available)
                - total_amount: Total trading amount (KRW)
            - avg_short_ratio: Average short ratio over the period
            - short_balance: Short balance data (if available)
                - balance_shares: Outstanding short shares
                - balance_amount: Outstanding short amount (KRW)
                - balance_ratio: Balance ratio (%)
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Short selling data is only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await naver_finance.fetch_short_interest(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="krx",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

    @mcp.tool(
        name="get_kimchi_premium",
        description="Get kimchi premium (김치 프리미엄) for cryptocurrencies. Compares Upbit KRW prices with Binance USDT prices to calculate the Korean exchange premium percentage.",
    )
    async def get_kimchi_premium(
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Get kimchi premium for cryptocurrencies.

        Args:
            symbol: Coin symbol (e.g., "BTC", "ETH"). If not specified,
                     returns data for major coins (BTC, ETH, XRP, SOL, etc.)

        Returns:
            Dictionary with kimchi premium data including exchange rate,
            Upbit/Binance prices, and premium percentage for each coin.
        """
        if symbol:
            sym = symbol.strip().upper()
            # Strip KRW- or USDT- prefix if provided
            if sym.startswith("KRW-"):
                sym = sym[4:]
            elif sym.startswith("USDT-"):
                sym = sym[5:]
            symbols = [sym]
        else:
            symbols = list(DEFAULT_KIMCHI_SYMBOLS)

        try:
            return await _fetch_kimchi_premium(symbols)
        except Exception as exc:
            return _error_payload(
                source="upbit+binance",
                message=str(exc),
                instrument_type="crypto",
            )
