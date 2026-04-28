"""Pure pending-order reconciliation service.

Read-only / decision-support only. This module must not import broker,
order-execution, watch-alert, paper-order, fill-notification, KIS-websocket,
DB, or Redis modules. Callers collect their own context (orders, quotes,
orderbook, support/resistance, KR universe) and pass it as plain DTOs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Classification = Literal[
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class PendingOrderInput:
    order_id: str
    symbol: str
    market: str
    side: str
    ordered_price: Decimal
    ordered_qty: Decimal
    remaining_qty: Decimal
    currency: str | None
    ordered_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteContext:
    price: Decimal
    as_of: datetime | None


@dataclass(frozen=True, slots=True)
class OrderbookLevelContext:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookContext:
    best_bid: OrderbookLevelContext | None
    best_ask: OrderbookLevelContext | None
    total_bid_qty: Decimal | None = None
    total_ask_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SupportResistanceLevel:
    price: Decimal
    distance_pct: Decimal


@dataclass(frozen=True, slots=True)
class SupportResistanceContext:
    nearest_support: SupportResistanceLevel | None
    nearest_resistance: SupportResistanceLevel | None


@dataclass(frozen=True, slots=True)
class KrUniverseContext:
    nxt_eligible: bool
    name: str | None = None
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class MarketContextInput:
    quote: QuoteContext | None
    orderbook: OrderbookContext | None
    support_resistance: SupportResistanceContext | None
    kr_universe: KrUniverseContext | None


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    near_fill_pct: Decimal = Decimal("0.5")
    too_far_pct: Decimal = Decimal("5.0")
    chasing_pct: Decimal = Decimal("3.0")
    chasing_resistance_pct: Decimal = Decimal("1.0")
    chasing_support_pct: Decimal = Decimal("1.0")
    quote_stale_seconds: int = 300


@dataclass(frozen=True, slots=True)
class PendingReconciliationItem:
    order_id: str
    symbol: str
    market: str
    side: str
    classification: Classification
    nxt_actionable: bool | None
    gap_pct: Decimal | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


_VALID_MARKETS = ("kr", "us", "crypto")
_VALID_SIDES = ("buy", "sell")
_CURRENCY_BY_MARKET = {
    "kr": frozenset({"KRW"}),
    "us": frozenset({"USD"}),
    "crypto": frozenset({"KRW", "USDT"}),
}


def _check_unknown_venue(
    order: PendingOrderInput,
    warnings: list[str],
) -> bool:
    bad = False
    if order.market not in _VALID_MARKETS:
        warnings.append("unknown_venue")
        bad = True
    if order.side not in _VALID_SIDES:
        warnings.append("unknown_side")
        bad = True
    return bad


def _check_data_mismatch(
    order: PendingOrderInput,
    reasons: list[str],
) -> bool:
    bad = False
    if order.ordered_price is None or order.ordered_price <= 0:
        reasons.append("non_positive_ordered_price")
        bad = True
    if order.remaining_qty is None or order.remaining_qty <= 0:
        reasons.append("non_positive_remaining_qty")
        bad = True
    if order.currency:
        allowed = _CURRENCY_BY_MARKET.get(order.market)
        if allowed is not None and order.currency.upper() not in allowed:
            reasons.append("currency_mismatch")
            bad = True
    return bad


def _resolve_nxt_actionable(
    order: PendingOrderInput,
    context: MarketContextInput,
    warnings: list[str],
) -> tuple[bool | None, bool]:
    """Return (nxt_actionable, is_kr_pending_non_nxt)."""
    if order.market != "kr":
        return None, False
    if context.kr_universe is None:
        warnings.append("missing_kr_universe")
        return None, False
    if context.kr_universe.nxt_eligible:
        return True, False
    warnings.append("non_nxt_venue")
    return False, True


def _empty_decision_support() -> dict[str, Decimal | str | None]:
    return {
        "current_price": None,
        "gap_pct": None,
        "signed_distance_to_fill": None,
        "nearest_support_price": None,
        "nearest_support_distance_pct": None,
        "nearest_resistance_price": None,
        "nearest_resistance_distance_pct": None,
        "bid_ask_spread_pct": None,
    }


def reconcile_pending_order(  # noqa: C901  (rule-by-rule classifier)
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> PendingReconciliationItem:
    cfg = config or ReconciliationConfig()
    warnings: list[str] = []
    reasons: list[str] = []
    decision_support = _empty_decision_support()

    if _check_unknown_venue(order, warnings):
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="unknown_venue",
            nxt_actionable=None,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if _check_data_mismatch(order, reasons):
        nxt_actionable, _ = _resolve_nxt_actionable(order, context, warnings)
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="data_mismatch",
            nxt_actionable=nxt_actionable,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    nxt_actionable, is_non_nxt = _resolve_nxt_actionable(order, context, warnings)
    if is_non_nxt:
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="kr_pending_non_nxt",
            nxt_actionable=False,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    quote = context.quote
    if quote is None:
        warnings.append("missing_quote")
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="unknown",
            nxt_actionable=nxt_actionable,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if quote.as_of is not None:
        reference = now or order.ordered_at
        if reference is not None:
            age = (reference - quote.as_of).total_seconds()
            if age > cfg.quote_stale_seconds:
                warnings.append("stale_quote")

    if context.orderbook is None:
        warnings.append("missing_orderbook")
    if context.support_resistance is None:
        warnings.append("missing_support_resistance")

    gap_pct = (quote.price - order.ordered_price) / order.ordered_price * Decimal("100")
    signed_distance_to_fill = -gap_pct if order.side == "buy" else gap_pct
    decision_support["current_price"] = quote.price
    decision_support["gap_pct"] = gap_pct
    decision_support["signed_distance_to_fill"] = signed_distance_to_fill

    sr = context.support_resistance
    if sr is not None:
        if sr.nearest_support is not None:
            decision_support["nearest_support_price"] = sr.nearest_support.price
            decision_support["nearest_support_distance_pct"] = (
                sr.nearest_support.distance_pct
            )
        if sr.nearest_resistance is not None:
            decision_support["nearest_resistance_price"] = sr.nearest_resistance.price
            decision_support["nearest_resistance_distance_pct"] = (
                sr.nearest_resistance.distance_pct
            )

    ob = context.orderbook
    if ob is not None and ob.best_bid is not None and ob.best_ask is not None:
        bid = ob.best_bid.price
        ask = ob.best_ask.price
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
            decision_support["bid_ask_spread_pct"] = spread_pct

    abs_gap = abs(gap_pct)
    classification: Classification

    if abs_gap <= cfg.near_fill_pct:
        classification = "near_fill"
        reasons.append("gap_within_near_fill_pct")
    elif signed_distance_to_fill < 0 and abs_gap >= cfg.too_far_pct:
        classification = "too_far"
        reasons.append("gap_against_fill_exceeds_too_far_pct")
    elif (
        signed_distance_to_fill > cfg.chasing_pct
        and context.support_resistance is not None
        and (
            (
                order.side == "buy"
                and context.support_resistance.nearest_resistance is not None
                and context.support_resistance.nearest_resistance.distance_pct
                <= cfg.chasing_resistance_pct
            )
            or (
                order.side == "sell"
                and context.support_resistance.nearest_support is not None
                and context.support_resistance.nearest_support.distance_pct
                <= cfg.chasing_support_pct
            )
        )
    ):
        classification = "chasing_risk"
        reasons.append(
            "price_diverged_into_resistance"
            if order.side == "buy"
            else "price_diverged_into_support"
        )
    else:
        classification = "maintain"

    return PendingReconciliationItem(
        order_id=order.order_id,
        symbol=order.symbol,
        market=order.market,
        side=order.side,
        classification=classification,
        nxt_actionable=nxt_actionable,
        gap_pct=gap_pct,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        decision_support=decision_support,
    )


def reconcile_pending_orders(
    orders: Sequence[PendingOrderInput],
    contexts_by_order_id: dict[str, MarketContextInput],
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> list[PendingReconciliationItem]:
    empty = MarketContextInput(
        quote=None,
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    return [
        reconcile_pending_order(
            order,
            contexts_by_order_id.get(order.order_id, empty),
            config=config,
            now=now,
        )
        for order in orders
    ]


__all__ = [
    "Classification",
    "PendingOrderInput",
    "QuoteContext",
    "OrderbookLevelContext",
    "OrderbookContext",
    "SupportResistanceLevel",
    "SupportResistanceContext",
    "KrUniverseContext",
    "MarketContextInput",
    "ReconciliationConfig",
    "PendingReconciliationItem",
    "reconcile_pending_order",
    "reconcile_pending_orders",
]
