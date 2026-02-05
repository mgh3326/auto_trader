from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from fastmcp import FastMCP

from app.core.db import AsyncSessionLocal
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient
from app.services.stock_info_service import StockInfoService


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
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "price": price,
        "source": "upbit",
    }


async def _fetch_quote_equity_kr(symbol: str, market: str | None) -> dict[str, Any]:
    """Fetch Korean equity quote from KIS."""
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol, market=(market or "J"), n=1
    )
    last = df.iloc[-1].to_dict() if not df.empty else {}
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "date": str(last.get("date")) if last.get("date") is not None else None,
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "close": last.get("close"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }


async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    """Fetch US equity quote from Yahoo Finance."""
    df = await yahoo_service.fetch_price(symbol)
    row = df.reset_index().iloc[-1].to_dict() if not df.empty else {}
    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "date": str(row.get("date")) if row.get("date") is not None else None,
        "time": str(row.get("time")) if row.get("time") is not None else None,
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "source": "yahoo",
    }


async def _fetch_ohlcv_crypto(symbol: str, days: int) -> dict[str, Any]:
    """Fetch crypto OHLCV from Upbit."""
    capped_days = min(days, 200)
    df = await upbit_service.fetch_ohlcv(market=symbol, days=capped_days)
    return {
        "symbol": symbol,
        "instrument_type": "crypto",
        "source": "upbit",
        "days": capped_days,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_kr(
    symbol: str, days: int, market: str | None
) -> dict[str, Any]:
    """Fetch Korean equity OHLCV from KIS."""
    capped_days = min(days, 200)
    kis = KISClient()
    df = await kis.inquire_daily_itemchartprice(
        code=symbol,
        market=(market or "J"),
        n=capped_days,
        period="D",
    )
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "source": "kis",
        "days": capped_days,
        "rows": _normalize_rows(df),
    }


async def _fetch_ohlcv_equity_us(symbol: str, days: int) -> dict[str, Any]:
    """Fetch US equity OHLCV from Yahoo Finance."""
    capped_days = min(days, 100)
    df = await yahoo_service.fetch_ohlcv(ticker=symbol, days=capped_days)
    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "yahoo",
        "days": capped_days,
        "rows": _normalize_rows(df),
    }


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol", description="Search symbols by query (symbol or name)."
    )
    async def search_symbol(query: str, limit: int = 20) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        try:
            async with AsyncSessionLocal() as db:
                svc = StockInfoService(db)
                rows = await svc.search_stocks(
                    query=query, limit=min(max(limit, 1), 100)
                )
        except Exception as exc:
            return [_error_payload(source="db", message=str(exc), query=query)]

        return [
            {
                "symbol": r.symbol,
                "name": r.name,
                "instrument_type": r.instrument_type,
                "exchange": r.exchange,
                "is_active": r.is_active,
            }
            for r in rows
        ]

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
                return await _fetch_quote_equity_kr(symbol, market)
            else:  # equity_us
                return await _fetch_quote_equity_us(symbol)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )

    @mcp.tool(name="get_ohlcv", description="Get OHLCV candles for a symbol.")
    async def get_ohlcv(
        symbol: str,
        days: int = 100,
        market: str | None = None,
    ) -> dict[str, Any]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        days = int(days)
        if days <= 0:
            raise ValueError("days must be > 0")

        market_type, symbol = _resolve_market_type(symbol, market)

        source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
        source = source_map[market_type]

        try:
            if market_type == "crypto":
                return await _fetch_ohlcv_crypto(symbol, days)
            elif market_type == "equity_kr":
                return await _fetch_ohlcv_equity_kr(symbol, days, market)
            else:  # equity_us
                return await _fetch_ohlcv_equity_us(symbol, days)
        except Exception as exc:
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=market_type,
            )
