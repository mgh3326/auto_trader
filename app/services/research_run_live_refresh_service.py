"""Live-refresh provider for Research Run → Trading Decision Session.

IMPURE: This module makes read-only calls to KIS/Upbit/market_data services.
It must NOT import any order mutation modules (see plan §2 forbidden list).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.schemas.research_run_decision_session import (
    KrUniverseSnapshot,
    LiveRefreshQuote,
    LiveRefreshSnapshot,
    OrderbookLevel,
    OrderbookSnapshot,
    PendingOrderSnapshot,
)

if TYPE_CHECKING:
    from app.models.research_run import ResearchRun


class LiveRefreshTimeout(Exception):
    """Raised when live refresh exceeds timeout."""


def _symbols_for_run(run: ResearchRun) -> set[str]:
    symbols = {candidate.symbol for candidate in run.candidates}
    symbols.update(recon.symbol for recon in run.reconciliations)
    return symbols


def _orderbook_level(level: Any | None) -> OrderbookLevel | None:
    if level is None:
        return None
    return OrderbookLevel(
        price=Decimal(str(level.price)),
        quantity=Decimal(str(level.quantity)),
    )


def _orderbook_snapshot(orderbook: Any) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid=_orderbook_level(orderbook.bids[0] if orderbook.bids else None),
        best_ask=_orderbook_level(orderbook.asks[0] if orderbook.asks else None),
        total_bid_qty=Decimal(str(orderbook.total_bid_qty))
        if orderbook.total_bid_qty
        else None,
        total_ask_qty=Decimal(str(orderbook.total_ask_qty))
        if orderbook.total_ask_qty
        else None,
    )


async def _fetch_quote(
    *,
    symbol: str,
    market_scope: str,
    timeout: float,
    now: Callable[[], datetime],
    quote_by_symbol: dict[str, LiveRefreshQuote],
    warnings: list[str],
) -> None:
    from app.services.market_data import get_quote

    try:
        quote = await asyncio.wait_for(
            get_quote(symbol, market=market_scope),
            timeout=timeout,
        )
        quote_by_symbol[symbol] = LiveRefreshQuote(
            price=Decimal(str(quote.price)),
            as_of=now(),
        )
    except Exception:
        warnings.append(f"quote_failed:{symbol}")


async def _fetch_orderbook(
    *,
    symbol: str,
    market_scope: str,
    timeout: float,
    orderbook_by_symbol: dict[str, OrderbookSnapshot],
    warnings: list[str],
) -> None:
    from app.services.market_data import get_orderbook

    if market_scope == "us":
        warnings.append("orderbook_unavailable_us")
        return
    try:
        orderbook = await asyncio.wait_for(
            get_orderbook(symbol, market=market_scope),
            timeout=timeout,
        )
        orderbook_by_symbol[symbol] = _orderbook_snapshot(orderbook)
    except Exception:
        warnings.append(f"orderbook_failed:{symbol}")


async def _fetch_kr_universe(
    *,
    db: AsyncSession,
    symbol: str,
    market_scope: str,
    kr_universe_by_symbol: dict[str, KrUniverseSnapshot],
    warnings: list[str],
) -> None:
    if market_scope != "kr":
        return
    try:
        result = await db.execute(
            select(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == symbol,
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
    except Exception:
        row = None

    if row is None:
        warnings.append(f"missing_kr_universe:{symbol}")
        return

    kr_universe_by_symbol[symbol] = KrUniverseSnapshot(
        nxt_eligible=row.nxt_eligible,
        name=row.name,
        exchange=row.exchange,
    )


def _pending_order_snapshot(
    order: dict[str, Any], market_scope: str
) -> PendingOrderSnapshot:
    return PendingOrderSnapshot(
        order_id=str(order.get("order_id", "")),
        symbol=str(order.get("symbol", "")),
        market=str(order.get("market", market_scope)),
        side=str(order.get("side", "buy")),
        ordered_price=Decimal(str(order.get("price", 0))),
        ordered_qty=Decimal(str(order.get("quantity", 0))),
        remaining_qty=Decimal(
            str(order.get("remaining_qty", order.get("quantity", 0)))
        ),
        currency=str(order.get("currency", "")),
        ordered_at=order.get("created_at"),
    )


async def _fetch_pending_orders(
    *,
    market_scope: str,
    timeout: float,
    warnings: list[str],
) -> list[PendingOrderSnapshot]:
    from app.mcp_server.tooling.orders_history import get_order_history_impl

    try:
        order_history = await asyncio.wait_for(
            get_order_history_impl(
                status="pending", market=market_scope, is_mock=False
            ),
            timeout=timeout,
        )
    except Exception:
        warnings.append("pending_orders_fetch_failed")
        return []

    pending_orders: list[PendingOrderSnapshot] = []
    for order in order_history.get("orders", []):
        try:
            pending_orders.append(_pending_order_snapshot(order, market_scope))
        except (ValueError, TypeError):
            warnings.append(
                f"pending_order_parse_failed:{order.get('order_id', 'unknown')}"
            )
    return pending_orders


async def build_live_refresh_snapshot(
    db: AsyncSession,
    *,
    run: ResearchRun,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    timeout_seconds: float = 8.0,
) -> LiveRefreshSnapshot:
    """Build a live market snapshot for decision session creation."""
    symbols = _symbols_for_run(run)
    per_call_timeout = timeout_seconds / max(len(symbols) * 3, 10)

    quote_by_symbol: dict[str, LiveRefreshQuote] = {}
    orderbook_by_symbol: dict[str, OrderbookSnapshot] = {}
    kr_universe_by_symbol: dict[str, KrUniverseSnapshot] = {}
    warnings: list[str] = []

    await asyncio.gather(
        *[
            task
            for symbol in symbols
            for task in (
                _fetch_quote(
                    symbol=symbol,
                    market_scope=run.market_scope,
                    timeout=per_call_timeout,
                    now=now,
                    quote_by_symbol=quote_by_symbol,
                    warnings=warnings,
                ),
                _fetch_orderbook(
                    symbol=symbol,
                    market_scope=run.market_scope,
                    timeout=per_call_timeout,
                    orderbook_by_symbol=orderbook_by_symbol,
                    warnings=warnings,
                ),
                _fetch_kr_universe(
                    db=db,
                    symbol=symbol,
                    market_scope=run.market_scope,
                    kr_universe_by_symbol=kr_universe_by_symbol,
                    warnings=warnings,
                ),
            )
        ],
        return_exceptions=True,
    )

    pending_orders = await _fetch_pending_orders(
        market_scope=run.market_scope,
        timeout=per_call_timeout * 3,
        warnings=warnings,
    )

    warnings.extend(["cash_unavailable", "holdings_unavailable"])
    return LiveRefreshSnapshot(
        refreshed_at=now(),
        quote_by_symbol=quote_by_symbol,
        orderbook_by_symbol=orderbook_by_symbol,
        kr_universe_by_symbol=kr_universe_by_symbol,
        cash_balances={},
        holdings_by_symbol={},
        pending_orders=pending_orders,
        warnings=warnings,
    )


__all__ = [
    "LiveRefreshTimeout",
    "build_live_refresh_snapshot",
]
