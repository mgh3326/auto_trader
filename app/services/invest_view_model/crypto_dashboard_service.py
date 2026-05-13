"""Read-only crypto dashboard view model for ROB-226."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_order import PendingOrder
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.schemas.invest_crypto import (
    CryptoDashboardMeta,
    CryptoDashboardResponse,
    CryptoHoldingSummary,
    CryptoInsightsSummary,
    CryptoMarketCard,
    CryptoPendingOrderItem,
    CryptoPendingOrdersSummary,
    CryptoRiskBadge,
    CryptoSourceState,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

TickerProvider = Callable[
    [list[str]], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]
]
OrderbookSpreadProvider = Callable[
    [list[str]], Awaitable[dict[str, float | None]] | dict[str, float | None]
]


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _base_symbol(symbol: str) -> str:
    normalized = str(symbol or "").upper()
    if normalized.startswith("KRW-"):
        return normalized.split("-", 1)[1]
    if normalized.endswith("-KRW"):
        return normalized.rsplit("-", 1)[0]
    return normalized


def _ticker_map(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        market = str(row.get("market") or "").upper()
        if market:
            mapped[market] = row
    return mapped


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pending_symbol_variants(symbols: Sequence[str]) -> set[str]:
    variants: set[str] = set()
    for symbol in symbols:
        upper = str(symbol).upper()
        base = _base_symbol(upper)
        variants.update({upper, base, f"KRW-{base}", f"{base}-KRW"})
    return variants


async def _default_ticker_provider(markets: list[str]) -> list[dict[str, Any]]:
    from app.services.brokers.upbit.client import fetch_multiple_tickers

    return await fetch_multiple_tickers(markets)


async def _default_orderbook_spread_provider(
    markets: list[str],
) -> dict[str, float | None]:
    from app.services.upbit_orderbook import fetch_multiple_orderbooks

    spreads: dict[str, float | None] = {}
    orderbooks = await fetch_multiple_orderbooks(markets)
    for market, book in orderbooks.items():
        units = list(book.get("orderbook_units") or [])
        if not units:
            spreads[str(market).upper()] = None
            continue
        best = units[0]
        ask = _float_or_none(best.get("ask_price"))
        bid = _float_or_none(best.get("bid_price"))
        if ask is None or bid is None or bid <= 0:
            spreads[str(market).upper()] = None
            continue
        spreads[str(market).upper()] = ((ask - bid) / bid) * 100
    return spreads


async def _load_active_krw_markets(
    db: AsyncSession, *, limit: int
) -> list[UpbitSymbolUniverse]:
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.quote_currency == "KRW",
            UpbitSymbolUniverse.is_active.is_(True),
        )
        .order_by(UpbitSymbolUniverse.market.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_pending_orders(
    db: AsyncSession,
    *,
    user_id: int,
    symbols: Sequence[str],
    limit: int = 20,
) -> list[PendingOrder]:
    variants = _pending_symbol_variants(symbols)
    if not variants:
        return []
    stmt = (
        select(PendingOrder)
        .where(
            PendingOrder.user_id == user_id,
            PendingOrder.market == "crypto",
            PendingOrder.venue == "upbit",
            PendingOrder.symbol.in_(sorted(variants)),
            or_(PendingOrder.status == "open", PendingOrder.status == "partial_fill"),
        )
        .order_by(PendingOrder.ordered_at.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _build_pending_summary(rows: Sequence[PendingOrder]) -> CryptoPendingOrdersSummary:
    items = [
        CryptoPendingOrderItem(
            orderId=row.broker_order_id,
            symbol=str(row.symbol).upper(),
            baseSymbol=_base_symbol(str(row.symbol)),
            side=row.side,
            orderType=row.order_type,
            price=_float_or_none(row.price),
            quantity=float(row.quantity or 0),
            filledQuantity=float(row.filled_quantity or 0),
            status=row.status,
            orderedAt=row.ordered_at,
            updatedAt=row.updated_at,
        )
        for row in rows
    ]
    return CryptoPendingOrdersSummary(
        items=items,
        emptyState=None if items else "no_pending_orders",
    )


async def build_crypto_dashboard(
    *,
    db: AsyncSession,
    user_id: int,
    resolver: RelationResolver | None = None,
    ticker_provider: TickerProvider | None = None,
    orderbook_spread_provider: OrderbookSpreadProvider | None = None,
    limit: int = 20,
    orderbook_limit: int = 5,
) -> CryptoDashboardResponse:
    """Build a read-only crypto dashboard without broker mutations or syncs."""
    now = datetime.now(UTC)
    warnings: list[str] = []
    sources: list[CryptoSourceState] = []
    limit = max(1, min(limit, 50))
    orderbook_limit = max(0, min(orderbook_limit, limit))

    universe = await _load_active_krw_markets(db, limit=limit)
    markets = [row.market.upper() for row in universe]

    tickers: dict[str, dict[str, Any]] = {}
    if markets:
        provider = ticker_provider or _default_ticker_provider
        try:
            tickers = _ticker_map(await _maybe_await(provider(markets)))
            sources.append(
                CryptoSourceState(
                    source="upbit_ticker",
                    state="supported",
                    label="Upbit ticker",
                    fetchedAt=now,
                )
            )
        except Exception:
            warnings.append("crypto_ticker_unavailable")
            sources.append(
                CryptoSourceState(
                    source="upbit_ticker", state="unavailable", label="Upbit ticker"
                )
            )

    spreads: dict[str, float | None] = {}
    if markets and orderbook_limit > 0:
        spread_provider = (
            orderbook_spread_provider or _default_orderbook_spread_provider
        )
        try:
            raw_spreads = await _maybe_await(spread_provider(markets[:orderbook_limit]))
            spreads = {str(k).upper(): v for k, v in dict(raw_spreads).items()}
            sources.append(
                CryptoSourceState(
                    source="upbit_orderbook",
                    state="supported",
                    label="Upbit orderbook",
                    fetchedAt=now,
                )
            )
        except Exception:
            warnings.append("crypto_orderbook_unavailable")
            sources.append(
                CryptoSourceState(
                    source="upbit_orderbook",
                    state="unavailable",
                    label="Upbit orderbook",
                )
            )

    pending_rows = await _load_pending_orders(db, user_id=user_id, symbols=markets)
    pending_summary = _build_pending_summary(pending_rows)
    pending_by_base = {
        item.baseSymbol for item in pending_summary.items if item.baseSymbol
    }

    cards: list[CryptoMarketCard] = []
    held_symbols: list[str] = []
    for row in universe:
        symbol = row.market.upper()
        base = row.base_currency.upper()
        ticker = tickers.get(symbol, {})
        direct_keys = {("crypto", symbol), ("crypto", base), ("crypto", f"{base}-KRW")}
        is_held = bool(
            resolver
            and (
                resolver.is_held("crypto", symbol)
                or resolver.is_held("crypto", base)
                or resolver.is_held("crypto", f"{base}-KRW")
                or bool(direct_keys & resolver.held)
            )
        )
        is_watched = bool(
            resolver
            and (
                resolver.is_watched("crypto", symbol)
                or resolver.is_watched("crypto", base)
                or resolver.is_watched("crypto", f"{base}-KRW")
                or bool(direct_keys & resolver.watch)
            )
        )
        if is_held:
            held_symbols.append(symbol)
        badges: list[CryptoRiskBadge] = []
        if is_held:
            badges.append(CryptoRiskBadge(kind="held", label="보유", severity="info"))
        if base in pending_by_base:
            badges.append(
                CryptoRiskBadge(
                    kind="pending_order", label="미체결", severity="warning"
                )
            )
        spread = spreads.get(symbol)
        if spread is not None and spread > 0.5:
            badges.append(
                CryptoRiskBadge(
                    kind="thin_orderbook",
                    label="호가 스프레드 주의",
                    severity="warning",
                )
            )
        if symbol not in tickers:
            badges.append(
                CryptoRiskBadge(
                    kind="data_unavailable", label="시세 없음", severity="warning"
                )
            )
        cards.append(
            CryptoMarketCard(
                symbol=symbol,
                baseSymbol=base,
                displayName=row.korean_name or row.english_name or base,
                priceKrw=_float_or_none(ticker.get("trade_price")),
                changeRate24h=_float_or_none(ticker.get("signed_change_rate")),
                changeAmount24h=_float_or_none(ticker.get("signed_change_price")),
                accTradePrice24h=_float_or_none(ticker.get("acc_trade_price_24h")),
                volume24h=_float_or_none(ticker.get("acc_trade_volume_24h")),
                orderbookSpreadPct=spread,
                isHeld=is_held,
                isWatched=is_watched,
                badges=badges,
            )
        )

    insights = CryptoInsightsSummary(
        badges=[
            badge
            for card in cards
            for badge in card.badges
            if badge.kind in {"thin_orderbook", "data_unavailable"}
        ][:5],
        notes=["읽기 전용 대시보드입니다. 주문/감시/동기화 작업은 실행하지 않습니다."],
    )

    return CryptoDashboardResponse(
        asOf=now,
        cards=cards,
        holdings=CryptoHoldingSummary(
            heldCount=len(held_symbols), symbols=held_symbols
        ),
        pendingOrders=pending_summary,
        insights=insights,
        meta=CryptoDashboardMeta(warnings=warnings, sources=sources),
    )
