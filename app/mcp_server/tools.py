from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from fastmcp import FastMCP

from app.core.db import AsyncSessionLocal
from app.services.stock_info_service import StockInfoService
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient


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

        # Crypto (Upbit)
        if _is_crypto_market(symbol):
            symbol = symbol.upper()
            try:
                prices = await upbit_service.fetch_multiple_current_prices([symbol])
                price = prices.get(symbol)
                return {
                    "symbol": symbol,
                    "instrument_type": "crypto",
                    "price": price,
                    "source": "upbit",
                }
            except Exception as exc:
                return _error_payload(
                    source="upbit",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="crypto",
                )

        # Korea equity (KIS)
        if _is_korean_equity_code(symbol):
            try:
                kis = KISClient()
                df = await kis.inquire_daily_itemchartprice(
                    code=symbol, market=(market or "J"), n=1
                )
                last = df.iloc[-1].to_dict() if not df.empty else {}
                return {
                    "symbol": symbol,
                    "instrument_type": "equity_kr",
                    "date": str(last.get("date"))
                    if last.get("date") is not None
                    else None,
                    "open": last.get("open"),
                    "high": last.get("high"),
                    "low": last.get("low"),
                    "close": last.get("close"),
                    "volume": last.get("volume"),
                    "value": last.get("value"),
                    "source": "kis",
                }
            except Exception as exc:
                return _error_payload(
                    source="kis",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="equity_kr",
                )

        # US equity (Yahoo)
        if _is_us_equity_symbol(symbol):
            try:
                df = await yahoo_service.fetch_price(symbol)
                row = df.reset_index().iloc[-1].to_dict() if not df.empty else {}
                return {
                    "symbol": symbol,
                    "instrument_type": "equity_us",
                    "date": str(row.get("date"))
                    if row.get("date") is not None
                    else None,
                    "time": str(row.get("time"))
                    if row.get("time") is not None
                    else None,
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "source": "yahoo",
                }
            except Exception as exc:
                return _error_payload(
                    source="yahoo",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="equity_us",
                )

        raise ValueError("Unsupported symbol format")

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

        # Crypto
        if _is_crypto_market(symbol):
            symbol = symbol.upper()
            try:
                df = await upbit_service.fetch_ohlcv(market=symbol, days=min(days, 200))
                return {
                    "symbol": symbol,
                    "instrument_type": "crypto",
                    "source": "upbit",
                    "days": min(days, 200),
                    "rows": _normalize_rows(df),
                }
            except Exception as exc:
                return _error_payload(
                    source="upbit",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="crypto",
                )

        # Korea equity
        if _is_korean_equity_code(symbol):
            try:
                kis = KISClient()
                df = await kis.inquire_daily_itemchartprice(
                    code=symbol,
                    market=(market or "J"),
                    n=min(days, 200),
                    period="D",
                )
                return {
                    "symbol": symbol,
                    "instrument_type": "equity_kr",
                    "source": "kis",
                    "days": min(days, 200),
                    "rows": _normalize_rows(df),
                }
            except Exception as exc:
                return _error_payload(
                    source="kis",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="equity_kr",
                )

        # US equity
        if _is_us_equity_symbol(symbol):
            try:
                df = await yahoo_service.fetch_ohlcv(ticker=symbol, days=min(days, 100))
                return {
                    "symbol": symbol,
                    "instrument_type": "equity_us",
                    "source": "yahoo",
                    "days": min(days, 100),
                    "rows": _normalize_rows(df),
                }
            except Exception as exc:
                return _error_payload(
                    source="yahoo",
                    message=str(exc),
                    symbol=symbol,
                    instrument_type="equity_us",
                )

        raise ValueError("Unsupported symbol format")
