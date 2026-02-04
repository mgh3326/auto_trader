from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_symbol", description="Search symbols by query (symbol or name)."
    )
    async def search_symbol(query: str, limit: int = 20) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        async with AsyncSessionLocal() as db:
            svc = StockInfoService(db)
            rows = await svc.search_stocks(query=query, limit=min(max(limit, 1), 100))

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
            prices = await upbit_service.fetch_multiple_current_prices([symbol.upper()])
            price = prices.get(symbol.upper())
            return {
                "symbol": symbol.upper(),
                "instrument_type": "crypto",
                "price": price,
                "source": "upbit",
            }

        # Korea equity (KIS)
        if _is_korean_equity_code(symbol):
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

        # US equity (Yahoo)
        if _is_us_equity_symbol(symbol):
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
            df = await upbit_service.fetch_ohlcv(
                market=symbol.upper(), days=min(days, 200)
            )
            return {
                "symbol": symbol.upper(),
                "instrument_type": "crypto",
                "source": "upbit",
                "days": min(days, 200),
                "rows": df.to_dict(orient="records"),
            }

        # Korea equity
        if _is_korean_equity_code(symbol):
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
                "rows": df.to_dict(orient="records"),
            }

        # US equity
        if _is_us_equity_symbol(symbol):
            df = await yahoo_service.fetch_ohlcv(ticker=symbol, days=min(days, 100))
            return {
                "symbol": symbol,
                "instrument_type": "equity_us",
                "source": "yahoo",
                "days": min(days, 100),
                "rows": df.to_dict(orient="records"),
            }

        raise ValueError("Unsupported symbol format")
