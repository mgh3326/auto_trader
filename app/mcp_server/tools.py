from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from fastmcp import FastMCP

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
    s = symbol.strip()
    return len(s) == 6 and s.isdigit()


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
            raise ValueError("korean equity symbols must be 6 digits")
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
        code=symbol, market="J", n=1  # J = 주식/ETF/ETN
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
                results.append({
                    "symbol": code,
                    "name": name,
                    "instrument_type": "equity_kr",
                    "exchange": "KOSPI",
                    "is_active": True,
                })
                if len(results) >= limit:
                    return results

        for name, code in kosdaq.items():
            if query_lower in name.lower() or query_upper in code:
                results.append({
                    "symbol": code,
                    "name": name,
                    "instrument_type": "equity_kr",
                    "exchange": "KOSDAQ",
                    "is_active": True,
                })
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
                results.append({
                    "symbol": symbol,
                    "name": name_kr or name_en or symbol,
                    "instrument_type": "equity_us",
                    "exchange": exchange,
                    "is_active": True,
                })
                if len(results) >= limit:
                    return results

    # 3. Crypto 검색
    if instrument_type is None or instrument_type == "crypto":
        try:
            crypto_maps = await get_or_refresh_maps()
            name_to_pair = crypto_maps.get("NAME_TO_PAIR_KR", {})
            for name, pair in name_to_pair.items():
                if query_lower in name.lower() or query_upper in pair.upper():
                    results.append({
                        "symbol": pair,
                        "name": name,
                        "instrument_type": "crypto",
                        "exchange": "Upbit",
                        "is_active": True,
                    })
                    if len(results) >= limit:
                        return results
        except Exception:
            pass  # crypto 데이터 로드 실패 시 무시

    return results


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
                return await _fetch_ohlcv_equity_us(symbol, count, period, parsed_end_date)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )
