"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot,
ADX, Stochastic RSI, OBV, Fibonacci).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.core.config import settings
from app.mcp_server.tooling.market_data_indicators import (
    IndicatorType,
    _compute_crypto_realtime_rsi_from_frame,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    error_payload_from_exception as _error_payload_from_exception,
)
from app.mcp_server.tooling.shared import (
    normalize_market as _normalize_market,
)
from app.mcp_server.tooling.shared import (
    normalize_rows as _normalize_rows,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.services import kis_ohlcv_cache
from app.services.brokers.kis.client import KISClient
from app.services.kr_hourly_candles_read_service import read_kr_hourly_candles_1h
from app.services.kr_symbol_universe_service import search_kr_symbols
from app.services.upbit_symbol_universe_service import search_upbit_symbols
from app.services.us_symbol_universe_service import search_us_symbols

if TYPE_CHECKING:
    from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Symbol Search
# ---------------------------------------------------------------------------


async def _search_master_data(
    query: str, limit: int, instrument_type: str | None = None
) -> list[dict[str, Any]]:
    """Search symbols across KRX, US, and Upbit master datasets."""
    results: list[dict[str, Any]] = []

    if instrument_type is None or instrument_type == "equity_kr":
        kr_results = await search_kr_symbols(query, limit)
        results.extend(kr_results)
        if len(results) >= limit:
            return results

    if instrument_type is None or instrument_type == "equity_us":
        remaining = limit - len(results)
        if remaining > 0:
            us_results = await search_us_symbols(query, remaining)
            results.extend(us_results)
            if len(results) >= limit:
                return results

    if instrument_type is None or instrument_type == "crypto":
        remaining = limit - len(results)
        if remaining > 0:
            crypto_results = await search_upbit_symbols(query, remaining)
            results.extend(crypto_results)
            if len(results) >= limit:
                return results

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
        market="UN",
        n=1,
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
    normalized_symbol = str(symbol or "").strip().upper()
    not_found_message = f"Symbol '{normalized_symbol}' not found"

    try:
        fast_info = await yahoo_service.fetch_fast_info(normalized_symbol)
    except Exception as exc:
        raise RuntimeError(
            f"Yahoo quote fetch failed for '{normalized_symbol}': {exc}"
        ) from exc

    close_raw = fast_info.get("close")
    if close_raw is None:
        raise ValueError(not_found_message) from None

    try:
        price = float(close_raw)
    except (TypeError, ValueError):
        raise ValueError(not_found_message) from None

    if price <= 0:
        raise ValueError(not_found_message)

    previous_close_raw = fast_info.get("previous_close")
    open_raw = fast_info.get("open")
    high_raw = fast_info.get("high")
    low_raw = fast_info.get("low")
    volume_raw = fast_info.get("volume")

    def _to_float_or_none(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_int_or_none(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(float(value))
        except (TypeError, ValueError):
            return None

    return {
        "symbol": normalized_symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": _to_float_or_none(previous_close_raw),
        "open": _to_float_or_none(open_raw),
        "high": _to_float_or_none(high_raw),
        "low": _to_float_or_none(low_raw),
        "volume": _to_int_or_none(volume_raw),
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


_KST = ZoneInfo("Asia/Seoul")


async def _fetch_ohlcv_equity_kr(
    symbol: str,
    count: int,
    period: str,
    end_date: datetime.datetime | None,
) -> dict[str, Any]:
    """Fetch Korean equity OHLCV from KIS."""
    capped_count = min(count, 200)
    kis = KISClient()

    if period == "day":

        async def _raw_fetch_day(requested_count: int):
            return await kis.inquire_daily_itemchartprice(
                code=symbol,
                market="UN",
                n=requested_count,
                period="D",
                end_date=end_date.date() if end_date else None,
            )

        use_cache = end_date is None and settings.kis_ohlcv_cache_enabled
        if use_cache:
            df = await kis_ohlcv_cache.get_candles(
                symbol=symbol,
                count=capped_count,
                period="day",
                raw_fetcher=_raw_fetch_day,
            )
        else:
            df = await _raw_fetch_day(capped_count)
    elif period == "1h":
        df = await read_kr_hourly_candles_1h(
            symbol=symbol,
            count=capped_count,
            end_date=end_date,
        )
    else:
        kis_period_map = {"week": "W", "month": "M"}
        df = await kis.inquire_daily_itemchartprice(
            code=symbol,
            market="UN",
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

        if market_type == "equity_us":
            return await _fetch_quote_equity_us(symbol)

        source_map = {"crypto": "upbit", "equity_kr": "kis"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_quote_crypto(symbol)
            return await _fetch_quote_equity_kr(symbol)
        except Exception as exc:
            return _error_payload_from_exception(
                source=source,
                exc=exc,
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_ohlcv",
        description=(
            "Get OHLCV candles for a symbol. Supports daily/weekly/monthly periods "
            "plus 4h for crypto, 1h for KR/US equity/crypto, and date-based pagination."
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
        if period not in ("day", "week", "month", "4h", "1h"):
            raise ValueError("period must be 'day', 'week', 'month', '4h', or '1h'")

        parsed_end_date: datetime.datetime | None = None
        if end_date:
            try:
                parsed_end_date = datetime.datetime.fromisoformat(end_date)
            except ValueError as exc:
                raise ValueError(
                    "end_date must be ISO format (e.g., '2024-01-15')"
                ) from exc

        market_type, symbol = _resolve_market_type(symbol, market)

        if period == "4h" and market_type != "crypto":
            raise ValueError("period '4h' is supported only for crypto")

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
            return _error_payload_from_exception(
                source=source,
                exc=exc,
                symbol=symbol,
                instrument_type=market_type,
            )

    async def _get_indicators_impl(
        symbol: str, indicators: list[str], market: str | None = None
    ) -> dict[str, Any]:
        """Calculate requested indicators for a symbol.

        Supported indicators:
        - adx: returns adx, plus_di, minus_di
        - stoch_rsi: returns k, d
        - obv: returns obv, signal, divergence
        """
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        normalized_symbol = _normalize_symbol_input(symbol, market)
        market_missing = market is None or not str(market).strip()
        if market_missing and normalized_symbol.isalpha():
            raise ValueError(
                "market is required for plain alphabetic symbols. Use market='us' "
                "for US equities, or provide KRW-/USDT- prefixed symbol for crypto."
            )

        if not indicators:
            raise ValueError("indicators list is required and cannot be empty")

        valid_indicators = {
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger",
            "atr",
            "pivot",
            "adx",
            "stoch_rsi",
            "obv",
        }
        normalized_indicators: list[IndicatorType] = []
        for ind in indicators:
            ind_lower = ind.lower().strip()
            if ind_lower not in valid_indicators:
                raise ValueError(
                    f"Invalid indicator '{ind}'. Valid options: {', '.join(sorted(valid_indicators))}"
                )
            normalized_indicators.append(cast(IndicatorType, ind_lower))

        market_type, symbol = _resolve_market_type(normalized_symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=250)

            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")

            close_fallback_price = (
                float(df["close"].iloc[-1]) if "close" in df.columns else None
            )
            current_price = close_fallback_price
            if market_type == "crypto":
                try:
                    prices = await upbit_service.fetch_multiple_current_prices([symbol])
                    ticker_price = prices.get(symbol)
                    if ticker_price is not None:
                        current_price = float(ticker_price)
                except Exception:
                    current_price = close_fallback_price

            indicator_results = _compute_indicators(df, normalized_indicators)

            if market_type == "crypto" and "rsi" in normalized_indicators:
                realtime_rsi = _compute_crypto_realtime_rsi_from_frame(
                    df, current_price
                )
                if realtime_rsi is not None:
                    indicator_results.setdefault("rsi", {})["14"] = realtime_rsi

            return {
                "symbol": symbol,
                "price": current_price,
                "instrument_type": market_type,
                "source": source,
                "indicators": indicator_results,
            }

        except Exception as exc:
            return _error_payload_from_exception(
                source=source,
                exc=exc,
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(
        name="get_indicators",
        description=(
            "Calculate technical indicators for a symbol. Available indicators: "
            "sma (Simple Moving Average), ema (Exponential Moving Average), "
            "rsi (Relative Strength Index), macd (MACD), bollinger (Bollinger Bands), "
            "atr (Average True Range), pivot (Pivot Points), "
            "adx (Average Directional Index - returns adx, plus_di, minus_di), "
            "stoch_rsi (Stochastic RSI - returns k, d), "
            "obv (On-Balance Volume - returns obv, signal, divergence)."
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
    "_fetch_quote_crypto",
    "_fetch_quote_equity_kr",
    "_fetch_quote_equity_us",
    "_fetch_ohlcv_crypto",
    "_fetch_ohlcv_equity_kr",
    "_fetch_ohlcv_equity_us",
    "MARKET_DATA_TOOL_NAMES",
    "_register_market_data_tools_impl",
]
