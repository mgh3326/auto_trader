from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import StockDetailOrder, StockDetailOrdersResponse
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    _normalize_crypto_market,
)

FilledOrdersFetcher = Callable[[int, list[NewsMarket]], Awaitable[list[dict[str, Any]]]]

logger = logging.getLogger(__name__)
_FILLED_ORDERS_TIMEOUT_SECONDS = 3


def _canonical_order_symbol(market: NewsMarket, symbol: str) -> str:
    if market == "us":
        return to_db_symbol(symbol.strip().upper())
    if market == "crypto":
        normalized = _normalize_crypto_market(symbol)
        if normalized.startswith("KRW-"):
            return normalized.removeprefix("KRW-")
        return normalized
    return symbol.strip().upper()


def _row_symbol(row: dict[str, Any], market: NewsMarket) -> str:
    raw = str(row.get("symbol") or row.get("ticker") or "")
    if market == "us":
        return to_db_symbol(raw.upper())
    if market == "crypto":
        normalized = raw.upper()
        if normalized.startswith("KRW-"):
            return normalized.removeprefix("KRW-")
        if normalized.endswith("-KRW"):
            return normalized.removesuffix("-KRW")
        return normalized
    return raw.upper()


async def _default_fetch_filled_orders(
    days: int, markets: list[NewsMarket]
) -> list[dict[str, Any]]:
    from app.services.filled_orders_service import fetch_filled_orders

    payload = await fetch_filled_orders(days=days, markets=",".join(markets))
    return list(payload.get("orders") or [])


async def build_stock_detail_orders(
    *,
    market: NewsMarket,
    symbol: str,
    fetcher: FilledOrdersFetcher = _default_fetch_filled_orders,
    days: int = 90,
    limit: int = 30,
    cursor: str | None = None,
    timeout_seconds: float = _FILLED_ORDERS_TIMEOUT_SECONDS,
) -> StockDetailOrdersResponse:
    days_clamped = max(1, min(days, 365))
    limit_clamped = max(1, min(limit, 100))
    offset = max(0, int(cursor or 0))
    canonical = _canonical_order_symbol(market, symbol)
    warnings: list[str] = []
    try:
        rows = await asyncio.wait_for(
            fetcher(days_clamped, [market]), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning(
            "stock-detail filled-orders fetch timed out: market=%s days=%s",
            market,
            days_clamped,
        )
        rows = []
        warnings.append("filled_orders_timeout")
    except Exception as exc:
        logger.warning(
            "stock-detail filled-orders fetch unavailable: market=%s error_type=%s",
            market,
            type(exc).__name__,
        )
        rows = []
        warnings.append("filled_orders_unavailable")
    filtered = [row for row in rows if _row_symbol(row, market) == canonical]
    page = filtered[offset : offset + limit_clamped]
    next_offset = offset + limit_clamped
    next_cursor = str(next_offset) if next_offset < len(filtered) else None

    items = [
        StockDetailOrder(
            orderId=str(row.get("order_id") or row.get("orderId") or "") or None,
            symbol=canonical
            if market != "crypto"
            else _canonical_order_symbol(market, symbol),
            market=market,
            side=str(row.get("side") or ""),
            quantity=float(row.get("quantity") or row.get("qty") or 0),
            price=float(row["price"]) if row.get("price") is not None else None,
            filledAt=row.get("filled_at") or row.get("filledAt"),
            account=row.get("account"),
            source=row.get("source"),
        )
        for row in page
    ]

    return StockDetailOrdersResponse(
        symbol=canonical
        if market != "crypto"
        else _canonical_order_symbol(market, symbol),
        market=market,
        items=items,
        nextCursor=next_cursor,
        meta={
            "emptyState": "no_filled_orders" if not filtered else None,
            "warnings": warnings,
        },
    )


__all__ = ["build_stock_detail_orders"]
