"""Market data utilities: quotes, OHLCV, and technical indicators.

This module contains functions for fetching market data (quotes, OHLCV candles)
and computing technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR, Pivot,
ADX, Stochastic RSI, OBV, Fibonacci).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
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
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.monitoring import build_yfinance_tracing_session
from app.services import kis_ohlcv_cache
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
    import yfinance as yf

    from app.core.symbol import to_yahoo_symbol

    yahoo_ticker = to_yahoo_symbol(symbol)
    session = build_yfinance_tracing_session()
    info = yf.Ticker(yahoo_ticker, session=session).fast_info

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


_KST = ZoneInfo("Asia/Seoul")
_KR_ROUTE_CLOSE = {
    "J": "153000",
    "UN": "200000",
}
_KR_ROUTE_START = {
    "J": "090000",
    "UN": "080000",
}
_KR_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_kr_symbol_universe.py"


def _kr_universe_sync_hint() -> str:
    return f"Sync required: {_KR_UNIVERSE_SYNC_COMMAND}"


async def _resolve_kr_intraday_route(symbol: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    async with AsyncSessionLocal() as db:
        query = select(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol == normalized_symbol
        )
        result = await db.execute(query)
        universe = result.scalar_one_or_none()
    if universe is None:
        async with AsyncSessionLocal() as db:
            has_any_rows_result = await db.execute(
                select(KRSymbolUniverse.symbol).limit(1)
            )
            has_any_rows = has_any_rows_result.scalar_one_or_none()
        if has_any_rows is None:
            raise ValueError(f"kr_symbol_universe is empty. {_kr_universe_sync_hint()}")
        raise ValueError(
            f"KR symbol '{normalized_symbol}' is not registered in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )
    if not universe.is_active:
        raise ValueError(
            f"KR symbol '{normalized_symbol}' is inactive in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )
    return "UN" if universe.nxt_eligible else "J"


def _resolve_kr_intraday_end_time(route_market: str, target_day: datetime.date) -> str:
    session_close = _KR_ROUTE_CLOSE[route_market]
    now_kst = datetime.datetime.now(_KST)
    if target_day < now_kst.date():
        return session_close
    now_hhmmss = now_kst.strftime("%H%M%S")
    return min(now_hhmmss, session_close)


def _filter_kr_intraday_session(
    frame: pd.DataFrame,
    route_market: str,
) -> pd.DataFrame:
    if frame.empty or "datetime" not in frame.columns:
        return frame
    out = frame.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        return out
    start = _KR_ROUTE_START[route_market]
    end = _KR_ROUTE_CLOSE[route_market]
    hhmmss = out["datetime"].dt.strftime("%H%M%S")
    return out.loc[(hhmmss >= start) & (hhmmss <= end)].reset_index(drop=True)


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
        route_market = await _resolve_kr_intraday_route(symbol)

        async def _raw_fetch_1h(requested_count: int):
            aggregate_fn = getattr(KISClient, "_aggregate_intraday_to_hour", None)
            target_count = max(int(requested_count), 1)
            current_day = (
                end_date.date() if end_date else datetime.datetime.now(_KST).date()
            )
            estimated_days = (target_count + 5) // 6
            max_fetch_days = min(max(estimated_days + 3, 3), 120)

            merged = pd.DataFrame()
            for _ in range(max_fetch_days):
                end_time = _resolve_kr_intraday_end_time(route_market, current_day)
                intraday = await kis.inquire_time_dailychartprice(
                    code=symbol,
                    market=route_market,
                    n=200,
                    end_date=current_day,
                    end_time=end_time,
                )
                intraday = _filter_kr_intraday_session(intraday, route_market)
                if callable(aggregate_fn):
                    hourly = aggregate_fn(intraday)
                else:
                    hourly = intraday

                if not hourly.empty:
                    merged = pd.concat([merged, hourly], ignore_index=True)
                    if "datetime" in merged.columns:
                        merged = (
                            merged.drop_duplicates(subset=["datetime"], keep="last")
                            .sort_values("datetime")
                            .reset_index(drop=True)
                        )
                    else:
                        merged = merged.drop_duplicates().reset_index(drop=True)
                    if len(merged) >= target_count:
                        break

                current_day = current_day - datetime.timedelta(days=1)

            return merged.tail(target_count).reset_index(drop=True)

        use_cache = end_date is None and settings.kis_ohlcv_cache_enabled
        if use_cache:
            df = await kis_ohlcv_cache.get_candles(
                symbol=symbol,
                count=capped_count,
                period="1h",
                raw_fetcher=_raw_fetch_1h,
                route=route_market,
            )
        else:
            df = await _raw_fetch_1h(capped_count)
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
            return _error_payload(
                source=source,
                message=str(exc),
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
            normalized_indicators.append(ind_lower)  # type: ignore[arg-type]

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
