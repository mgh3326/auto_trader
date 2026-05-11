from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_home import InvestHomeResponse
from app.schemas.invest_stock_detail import (
    StockDetailBlockStates,
    StockDetailHolding,
    StockDetailLatestAnalysis,
    StockDetailOrderbook,
    StockDetailQuote,
    StockDetailResponse,
    StockDetailScreenerSnapshot,
    StockDetailValuation,
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


def _home_market_for_detail(market: NewsMarket) -> str:
    if market == "kr":
        return "KR"
    if market == "us":
        return "US"
    return "CRYPTO"


def _coerce_holding_from_home(
    holding: Any, *, market: NewsMarket, symbol: str
) -> StockDetailHolding | None:
    if holding is None:
        return None
    if isinstance(holding, StockDetailHolding):
        return holding

    home: InvestHomeResponse | None = None
    if isinstance(holding, InvestHomeResponse):
        home = holding
    elif isinstance(holding, dict) and "groupedHoldings" in holding:
        home = InvestHomeResponse.model_validate(holding)

    if home is not None:
        detail_market = _home_market_for_detail(market)
        grouped = next(
            (
                item
                for item in home.groupedHoldings
                if item.symbol == symbol and item.market == detail_market
            ),
            None,
        )
        if grouped is None:
            return None
        account_names = {account.accountId: account.displayName for account in home.accounts}
        return StockDetailHolding(
            totalQuantity=grouped.totalQuantity,
            averageCost=grouped.averageCost,
            costBasis=grouped.costBasis,
            valueNative=grouped.valueNative,
            valueKrw=grouped.valueKrw,
            pnlKrw=grouped.pnlKrw,
            pnlRate=grouped.pnlRate,
            includedSources=grouped.includedSources,
            sourceBreakdown=[
                {
                    "source": source.source,
                    "accountName": account_names.get(source.accountId),
                    "quantity": source.quantity,
                    "averageCost": source.averageCost,
                    "costBasis": source.costBasis,
                    "valueNative": source.valueNative,
                    "valueKrw": source.valueKrw,
                }
                for source in grouped.sourceBreakdown
            ],
            priceState=grouped.priceState,
        )

    return StockDetailHolding.model_validate(holding)


def _stock_detail_block_states(
    *,
    quote: StockDetailQuote | None,
    screener_snapshot: StockDetailScreenerSnapshot | None,
    valuation: StockDetailValuation | None,
    holding: StockDetailHolding | None,
    latest_analysis: StockDetailLatestAnalysis | None,
    orderbook: StockDetailOrderbook | None,
    orderbook_supported: bool,
    warnings: list[str],
) -> StockDetailBlockStates:
    def optional_state(block_name: str, value: object | None) -> str:
        if f"{block_name}_timeout" in warnings or f"{block_name}_unavailable" in warnings:
            return "error"
        return "fresh" if value is not None else "provider_unwired"

    screener_state = optional_state("screener_snapshot", screener_snapshot)
    if screener_snapshot is not None:
        screener_state = screener_snapshot.freshness

    valuation_state = optional_state("valuation", valuation)
    if valuation is not None:
        valuation_state = "fresh" if valuation.freshness == "ok" else valuation.freshness

    if orderbook_supported:
        orderbook_state = "fresh" if orderbook is not None else "missing"
    else:
        orderbook_state = "unsupported"

    return StockDetailBlockStates(
        quote=optional_state("quote", quote),
        screenerSnapshot=screener_state,
        valuation=valuation_state,
        holding=optional_state("holding", holding),
        latestAnalysis=optional_state("latest_analysis", latest_analysis),
        orderbook=orderbook_state,
    )


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
    if screener_snapshot is not None and not isinstance(
        screener_snapshot, StockDetailScreenerSnapshot
    ):
        screener_snapshot = StockDetailScreenerSnapshot.model_validate(screener_snapshot)
    if valuation is not None and not isinstance(valuation, StockDetailValuation):
        valuation = StockDetailValuation.model_validate(valuation)
    holding = _coerce_holding_from_home(
        holding, market=market, symbol=resolved.symbol_db
    )
    if latest_analysis is not None and not isinstance(
        latest_analysis, StockDetailLatestAnalysis
    ):
        latest_analysis = StockDetailLatestAnalysis.model_validate(latest_analysis)
    if orderbook is not None and not isinstance(orderbook, StockDetailOrderbook):
        orderbook = StockDetailOrderbook.model_validate(orderbook)

    block_states = _stock_detail_block_states(
        quote=quote,
        screener_snapshot=screener_snapshot,
        valuation=valuation,
        holding=holding,
        latest_analysis=latest_analysis,
        orderbook=orderbook,
        orderbook_supported=orderbook_support.supported,
        warnings=warnings,
    )

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
        meta={
            "computedAt": datetime.now(UTC),
            "warnings": warnings,
            "blockStates": block_states,
        },
    )


__all__ = ["build_stock_detail"]
