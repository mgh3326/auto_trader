"""Live-refresh provider for Research Run → Trading Decision Session.

IMPURE: This module makes read-only calls to KIS/Upbit/market_data services.
It must NOT import any order mutation modules (see plan §2 forbidden list).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

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

    pass


async def build_live_refresh_snapshot(
    db: AsyncSession,
    *,
    run: ResearchRun,
    user_id: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    timeout_seconds: float = 8.0,
) -> LiveRefreshSnapshot:
    """Build a live market snapshot for decision session creation.

    Args:
        db: Database session
        run: ResearchRun to refresh
        user_id: User ID for context
        now: Callable returning current datetime
        timeout_seconds: Total timeout for the refresh operation

    Returns:
        LiveRefreshSnapshot with all fetched data

    Raises:
        LiveRefreshTimeout: If the refresh exceeds timeout_seconds
    """
    from app.mcp_server.tooling.orders_history import get_order_history_impl
    from app.services.market_data import get_orderbook, get_quote

    # Collect unique symbols from candidates and reconciliations
    symbols = set()
    for candidate in run.candidates:
        symbols.add(candidate.symbol)
    for recon in run.reconciliations:
        symbols.add(recon.symbol)

    per_call_timeout = timeout_seconds / max(len(symbols) * 3, 10)

    quote_by_symbol: dict[str, LiveRefreshQuote] = {}
    orderbook_by_symbol: dict[str, OrderbookSnapshot] = {}
    kr_universe_by_symbol: dict[str, KrUniverseSnapshot] = {}
    warnings: list[str] = []

    async def fetch_quote_with_timeout(symbol: str) -> None:
        try:
            quote = await asyncio.wait_for(
                get_quote(symbol, market=run.market_scope),
                timeout=per_call_timeout,
            )
            quote_by_symbol[symbol] = LiveRefreshQuote(
                price=Decimal(str(quote.price)),
                as_of=now(),
            )
        except Exception:
            warnings.append(f"quote_failed:{symbol}")

    async def fetch_orderbook_with_timeout(symbol: str) -> None:
        if run.market_scope == "us":
            warnings.append("orderbook_unavailable_us")
            return
        try:
            ob = await asyncio.wait_for(
                get_orderbook(symbol, market=run.market_scope),
                timeout=per_call_timeout,
            )
            best_bid = None
            best_ask = None
            if ob.bids:
                best_bid = OrderbookLevel(
                    price=Decimal(str(ob.bids[0].price)),
                    quantity=Decimal(str(ob.bids[0].quantity)),
                )
            if ob.asks:
                best_ask = OrderbookLevel(
                    price=Decimal(str(ob.asks[0].price)),
                    quantity=Decimal(str(ob.asks[0].quantity)),
                )
            orderbook_by_symbol[symbol] = OrderbookSnapshot(
                best_bid=best_bid,
                best_ask=best_ask,
                total_bid_qty=Decimal(str(ob.total_bid_qty))
                if ob.total_bid_qty
                else None,
                total_ask_qty=Decimal(str(ob.total_ask_qty))
                if ob.total_ask_qty
                else None,
            )
        except Exception:
            warnings.append(f"orderbook_failed:{symbol}")

    async def fetch_kr_universe_with_timeout(symbol: str) -> None:
        if run.market_scope != "kr":
            return
        try:
            # Query KRSymbolUniverse directly to distinguish missing rows from non-NXT
            from sqlalchemy import select

            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = select(KRSymbolUniverse).where(
                KRSymbolUniverse.symbol == symbol,
                KRSymbolUniverse.is_active.is_(True),
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                # Symbol is missing from universe - omit from kr_universe_by_symbol
                # and emit warning for fail-closed handling (ROB-29)
                warnings.append(f"missing_kr_universe:{symbol}")
                return

            # Symbol exists - include in snapshot with its NXT eligibility
            kr_universe_by_symbol[symbol] = KrUniverseSnapshot(
                nxt_eligible=row.nxt_eligible,
                name=row.name,
                exchange=row.exchange,
            )
        except Exception:
            warnings.append(f"missing_kr_universe:{symbol}")

    # Fetch quotes, orderbooks, and KR universe data
    tasks = []
    for symbol in symbols:
        tasks.append(fetch_quote_with_timeout(symbol))
        tasks.append(fetch_orderbook_with_timeout(symbol))
        if run.market_scope == "kr":
            tasks.append(fetch_kr_universe_with_timeout(symbol))

    await asyncio.gather(*tasks, return_exceptions=True)

    # Fetch pending orders
    pending_orders: list[PendingOrderSnapshot] = []
    try:
        order_history = await asyncio.wait_for(
            get_order_history_impl(
                status="pending",
                market=run.market_scope,
                is_mock=False,
            ),
            timeout=per_call_timeout * 3,
        )
        for order in order_history.get("orders", []):
            try:
                pending_orders.append(
                    PendingOrderSnapshot(
                        order_id=str(order.get("order_id", "")),
                        symbol=str(order.get("symbol", "")),
                        market=str(order.get("market", run.market_scope)),
                        side=str(order.get("side", "buy")),
                        ordered_price=Decimal(str(order.get("price", 0))),
                        ordered_qty=Decimal(str(order.get("quantity", 0))),
                        remaining_qty=Decimal(
                            str(order.get("remaining_qty", order.get("quantity", 0)))
                        ),
                        currency=str(order.get("currency", "")),
                        ordered_at=order.get("created_at"),
                    )
                )
            except (ValueError, TypeError):
                warnings.append(
                    f"pending_order_parse_failed:{order.get('order_id', 'unknown')}"
                )
    except Exception:
        warnings.append("pending_orders_fetch_failed")

    # Fetch cash balances and holdings (read-only fetches per plan §4.3)
    # For now, emit explicit warnings as these are not yet implemented
    cash_balances: dict[str, Decimal] = {}
    holdings_by_symbol: dict[str, Decimal] = {}

    # Emit warnings for unavailable cash/holdings data
    warnings.append("cash_unavailable")
    warnings.append("holdings_unavailable")

    # Set refreshed_at after all fetches complete
    refreshed_at = now()

    return LiveRefreshSnapshot(
        refreshed_at=refreshed_at,
        quote_by_symbol=quote_by_symbol,
        orderbook_by_symbol=orderbook_by_symbol,
        kr_universe_by_symbol=kr_universe_by_symbol,
        cash_balances=cash_balances,
        holdings_by_symbol=holdings_by_symbol,
        pending_orders=pending_orders,
        warnings=warnings,
    )


__all__ = [
    "LiveRefreshTimeout",
    "build_live_refresh_snapshot",
]
