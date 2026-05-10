from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    StockDetailHolding,
    StockDetailLatestAnalysis,
    StockDetailOrderbook,
    StockDetailQuote,
    StockDetailResponse,
    default_capabilities_for_market,
    orderbook_support_for_market,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    ResolvedSymbol,
    resolve_symbol,
)

logger = logging.getLogger(__name__)

Resolver = Callable[[NewsMarket, str, AsyncSession], Awaitable[ResolvedSymbol]]
Provider = Callable[..., Awaitable[Any]]


async def _none_provider(*args: Any, **kwargs: Any) -> None:
    return None


async def _run_optional_block(
    name: str, coro: Awaitable[Any], warnings: list[str]
) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=3)
    except TimeoutError:
        warnings.append(f"{name}_timeout")
    except Exception as exc:  # pragma: no cover - exercised by callers with stubs
        logger.warning("stock-detail %s block unavailable: %s", name, exc)
        warnings.append(f"{name}_unavailable")
    return None


async def build_stock_detail(
    *,
    user_id: int | str,
    market: NewsMarket,
    symbol: str,
    db: AsyncSession,
    resolver: Resolver = resolve_symbol,
    quote_provider: Provider = _none_provider,
    screener_provider: Provider = _none_provider,
    valuation_provider: Provider = _none_provider,
    holding_provider: Provider = _none_provider,
    latest_analysis_provider: Provider = _none_provider,
    orderbook_provider: Provider = _none_provider,
) -> StockDetailResponse:
    """Build the read-only above-the-fold stock-detail view-model.

    The default implementation deliberately degrades optional blocks to null
    unless a provider is wired. This keeps the router safe/read-only while the
    endpoint contract is stable; concrete providers can reuse existing services
    without changing the transport shape.
    """

    resolved = await resolver(market, symbol, db)
    warnings: list[str] = []

    quote_task = _run_optional_block(
        "quote", quote_provider(market, resolved.symbol_db, db), warnings
    )
    screener_task = _run_optional_block(
        "screener_snapshot",
        screener_provider(market, resolved.symbol_db, db),
        warnings,
    )
    valuation_task = _run_optional_block(
        "valuation", valuation_provider(market, resolved.symbol_db, db), warnings
    )
    holding_task = _run_optional_block(
        "holding",
        holding_provider(user_id, market, resolved.symbol_db, db),
        warnings,
    )
    latest_analysis_task = _run_optional_block(
        "latest_analysis",
        latest_analysis_provider(market, resolved.symbol_db, db),
        warnings,
    )
    if market == "kr":
        orderbook_task = _run_optional_block(
            "orderbook", orderbook_provider(market, resolved.symbol_db, db), warnings
        )
    else:
        orderbook_task = _none_provider()

    (
        quote,
        screener_snapshot,
        valuation,
        holding,
        latest_analysis,
        orderbook,
    ) = await asyncio.gather(
        quote_task,
        screener_task,
        valuation_task,
        holding_task,
        latest_analysis_task,
        orderbook_task,
    )

    capabilities = default_capabilities_for_market(market)
    orderbook_support = orderbook_support_for_market(market)
    if market == "kr" and orderbook is None:
        orderbook_support = orderbook_support.model_copy(
            update={"supported": False, "reason": "kr_unavailable"}
        )
        capabilities = capabilities.model_copy(
            update={
                "orderbook": capabilities.orderbook.model_copy(
                    update={"supported": False, "reason": "kr_unavailable"}
                )
            }
        )

    if quote is not None and not isinstance(quote, StockDetailQuote):
        quote = StockDetailQuote.model_validate(quote)
    if holding is not None and not isinstance(holding, StockDetailHolding):
        holding = StockDetailHolding.model_validate(holding)
    if latest_analysis is not None and not isinstance(
        latest_analysis, StockDetailLatestAnalysis
    ):
        latest_analysis = StockDetailLatestAnalysis.model_validate(latest_analysis)
    if orderbook is not None and not isinstance(orderbook, StockDetailOrderbook):
        orderbook = StockDetailOrderbook.model_validate(orderbook)

    return StockDetailResponse(
        symbol=resolved.symbol_db,
        market=market,
        displayName=resolved.display_name,
        exchange=resolved.exchange,
        instrumentType=resolved.instrument_type,
        currency=resolved.currency,
        assetType=resolved.asset_type,
        assetCategory=resolved.asset_category,
        quote=quote,
        screenerSnapshot=screener_snapshot,
        valuation=valuation,
        holding=holding,
        latestAnalysis=latest_analysis,
        orderbookSupport=orderbook_support,
        orderbook=orderbook,
        capabilities=capabilities,
        meta={"computedAt": datetime.now(UTC), "warnings": warnings},
    )


__all__ = ["build_stock_detail"]
