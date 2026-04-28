"""Pure NXT-specific classifier for KR pending orders, candidates, holdings.

Read-only / decision-support only. This module must not import broker,
order-execution, watch-alert, paper-order, fill-notification, KIS-websocket,
DB, or Redis modules. Callers collect their own context (orders, candidates,
holdings, quotes, orderbook, support/resistance, KR NXT eligibility) and
pass it as plain DTOs.

ROB-23 builds on `app.services.pending_reconciliation_service` (ROB-22).
Pending-order and candidate classification delegates to
`reconcile_pending_order(...)` and re-labels the result for NXT semantics.
Holding classification is independent (no fill-vs-market gap).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.services.pending_reconciliation_service import (
    MarketContextInput,
    OrderbookContext,
    PendingOrderInput,
    PendingReconciliationItem,
    ReconciliationConfig,
    reconcile_pending_order,
)

NxtClassification = Literal[
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
]
NxtKind = Literal["pending_order", "candidate", "holding"]


@dataclass(frozen=True, slots=True)
class NxtCandidateInput:
    candidate_id: str
    symbol: str
    side: str
    proposed_price: Decimal
    proposed_qty: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtHoldingInput:
    holding_id: str
    symbol: str
    quantity: Decimal
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtClassifierConfig:
    near_support_pct: Decimal = Decimal("1.0")
    near_resistance_pct: Decimal = Decimal("1.0")
    wide_spread_pct: Decimal = Decimal("1.0")
    thin_liquidity_total_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NxtClassifierItem:
    item_id: str
    symbol: str
    kind: NxtKind
    side: str | None
    classification: NxtClassification
    nxt_actionable: bool
    summary: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


_NXT_ACTIONABLE_LABELS: frozenset[NxtClassification] = frozenset(
    {
        "buy_pending_at_support",
        "buy_pending_actionable",
        "sell_pending_near_resistance",
        "sell_pending_actionable",
    }
)


def _is_nxt_actionable(label: NxtClassification) -> bool:
    return label in _NXT_ACTIONABLE_LABELS


def _format_price(value: object) -> str:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return ""


def _build_summary(  # noqa: C901 (rule-by-rule mapper)
    classification: NxtClassification,
    decision_support: dict[str, Decimal | str | None],
) -> str:
    if classification == "buy_pending_at_support":
        price = _format_price(decision_support.get("nearest_support_price"))
        if price:
            return f"NXT 매수 대기 — 지지선 근접 (지지선 {price})"
        return "NXT 매수 대기 — 적정 (지속 모니터링)"
    if classification == "buy_pending_actionable":
        return "NXT 매수 대기 — 적정 (지속 모니터링)"
    if classification == "buy_pending_too_far":
        return "NXT 매수 대기 — 시장가 대비 이격 큼 (재검토 필요)"
    if classification == "sell_pending_near_resistance":
        price = _format_price(decision_support.get("nearest_resistance_price"))
        if price:
            return f"NXT 매도 대기 — 저항선 근접 (저항선 {price})"
        return "NXT 매도 대기 — 적정 (지속 모니터링)"
    if classification == "sell_pending_actionable":
        return "NXT 매도 대기 — 적정 (지속 모니터링)"
    if classification == "sell_pending_too_optimistic":
        return "NXT 매도 대기 — 시장가 대비 너무 낙관적 (재검토 필요)"
    if classification == "non_nxt_pending_ignore_for_nxt":
        return "KR 일반종목 — NXT 대상 아님 (NXT 의사결정에서 제외)"
    if classification == "holding_watch_only":
        return "NXT 보유 — 신규 액션 없음, 모니터링 대상"
    if classification == "data_mismatch_requires_review":
        return "주문/포지션 데이터 불일치 — 운영자 검토 필요"
    return "NXT 분류 불가 — 시세 정보 부족"


def _is_kr_missing_universe(market: str, recon: PendingReconciliationItem) -> bool:
    return market == "kr" and "missing_kr_universe" in recon.warnings


def _far_or_chasing_label(side: str) -> NxtClassification:
    if side == "buy":
        return "buy_pending_too_far"
    return "sell_pending_too_optimistic"


def _pct_distance(reference: Decimal, target: Decimal) -> Decimal | None:
    if reference <= 0:
        return None
    return abs(reference - target) / reference * Decimal("100")


def _buy_proximity_label(
    recon: PendingReconciliationItem,
    order_price: Decimal,
    nxt_cfg: NxtClassifierConfig,
    extra_reasons: list[str],
) -> NxtClassification:
    support_price = recon.decision_support.get("nearest_support_price")
    if isinstance(support_price, Decimal):
        distance_pct = _pct_distance(order_price, support_price)
        if distance_pct is not None and distance_pct <= nxt_cfg.near_support_pct:
            extra_reasons.append("order_within_near_support_pct")
            return "buy_pending_at_support"
    return "buy_pending_actionable"


def _sell_proximity_label(
    recon: PendingReconciliationItem,
    order_price: Decimal,
    nxt_cfg: NxtClassifierConfig,
    extra_reasons: list[str],
) -> NxtClassification:
    resistance_price = recon.decision_support.get("nearest_resistance_price")
    if isinstance(resistance_price, Decimal):
        distance_pct = _pct_distance(order_price, resistance_price)
        if distance_pct is not None and distance_pct <= nxt_cfg.near_resistance_pct:
            extra_reasons.append("order_within_near_resistance_pct")
            return "sell_pending_near_resistance"
    return "sell_pending_actionable"


def _map_recon_to_nxt(
    recon: PendingReconciliationItem,
    *,
    market: str,
    side: str,
    order_price: Decimal,
    nxt_cfg: NxtClassifierConfig,
) -> tuple[NxtClassification, list[str]]:
    extra_reasons: list[str] = []
    if recon.classification in ("unknown_venue", "data_mismatch"):
        return "data_mismatch_requires_review", extra_reasons
    if recon.classification == "kr_pending_non_nxt":
        return "non_nxt_pending_ignore_for_nxt", extra_reasons
    # ROB-29 fail-closed: KR pending with no resolvable NXT-eligibility row must
    # NEVER default to actionable. Fires before any quote / S-R rule.
    if _is_kr_missing_universe(market, recon):
        extra_reasons.append("missing_kr_universe_fail_closed")
        return "data_mismatch_requires_review", extra_reasons
    if recon.classification == "unknown":
        return "unknown", extra_reasons
    if recon.classification in ("too_far", "chasing_risk"):
        return _far_or_chasing_label(side), extra_reasons
    if side == "buy":
        return _buy_proximity_label(
            recon, order_price, nxt_cfg, extra_reasons
        ), extra_reasons
    return _sell_proximity_label(
        recon, order_price, nxt_cfg, extra_reasons
    ), extra_reasons


def _apply_orderbook_warnings(
    decision_support: dict[str, Decimal | str | None],
    orderbook: OrderbookContext | None,
    nxt_cfg: NxtClassifierConfig,
    warnings: list[str],
) -> None:
    spread = decision_support.get("bid_ask_spread_pct")
    if isinstance(spread, Decimal) and spread > nxt_cfg.wide_spread_pct:
        warnings.append("wide_spread")
    if orderbook is not None and nxt_cfg.thin_liquidity_total_qty is not None:
        bid_total = orderbook.total_bid_qty or Decimal("0")
        ask_total = orderbook.total_ask_qty or Decimal("0")
        if bid_total + ask_total < nxt_cfg.thin_liquidity_total_qty:
            warnings.append("thin_liquidity")


def _holding_decision_support(
    context: MarketContextInput,
) -> dict[str, Decimal | str | None]:
    ds: dict[str, Decimal | str | None] = {
        "current_price": None,
        "gap_pct": None,
        "signed_distance_to_fill": None,
        "nearest_support_price": None,
        "nearest_support_distance_pct": None,
        "nearest_resistance_price": None,
        "nearest_resistance_distance_pct": None,
        "bid_ask_spread_pct": None,
    }
    if context.quote is not None:
        ds["current_price"] = context.quote.price
    sr = context.support_resistance
    if sr is not None:
        if sr.nearest_support is not None:
            ds["nearest_support_price"] = sr.nearest_support.price
            ds["nearest_support_distance_pct"] = sr.nearest_support.distance_pct
        if sr.nearest_resistance is not None:
            ds["nearest_resistance_price"] = sr.nearest_resistance.price
            ds["nearest_resistance_distance_pct"] = sr.nearest_resistance.distance_pct
    ob = context.orderbook
    if ob is not None and ob.best_bid is not None and ob.best_ask is not None:
        bid = ob.best_bid.price
        ask = ob.best_ask.price
        if bid > 0 and ask > 0:
            ds["bid_ask_spread_pct"] = (
                (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
            )
    return ds


def classify_nxt_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    recon = reconcile_pending_order(
        order, context, config=reconciliation_config, now=now
    )
    classification, extra_reasons = _map_recon_to_nxt(
        recon,
        market=order.market,
        side=order.side,
        order_price=order.ordered_price,
        nxt_cfg=nxt_cfg,
    )
    warnings = list(recon.warnings)
    _apply_orderbook_warnings(
        recon.decision_support, context.orderbook, nxt_cfg, warnings
    )
    return NxtClassifierItem(
        item_id=order.order_id,
        symbol=order.symbol,
        kind="pending_order",
        side=order.side,
        classification=classification,
        nxt_actionable=_is_nxt_actionable(classification),
        summary=_build_summary(classification, recon.decision_support),
        reasons=tuple(list(recon.reasons) + extra_reasons),
        warnings=tuple(warnings),
        decision_support=recon.decision_support,
    )


def classify_nxt_candidate(
    candidate: NxtCandidateInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    proxy_qty = (
        candidate.proposed_qty
        if candidate.proposed_qty is not None and candidate.proposed_qty > 0
        else Decimal("1")
    )
    proxy_order = PendingOrderInput(
        order_id=candidate.candidate_id,
        symbol=candidate.symbol,
        market="kr",
        side=candidate.side,
        ordered_price=candidate.proposed_price,
        ordered_qty=proxy_qty,
        remaining_qty=proxy_qty,
        currency=candidate.currency,
        ordered_at=None,
    )
    recon = reconcile_pending_order(
        proxy_order, context, config=reconciliation_config, now=now
    )
    classification, extra_reasons = _map_recon_to_nxt(
        recon,
        market="kr",
        side=candidate.side,
        order_price=candidate.proposed_price,
        nxt_cfg=nxt_cfg,
    )
    warnings = list(recon.warnings)
    _apply_orderbook_warnings(
        recon.decision_support, context.orderbook, nxt_cfg, warnings
    )
    return NxtClassifierItem(
        item_id=candidate.candidate_id,
        symbol=candidate.symbol,
        kind="candidate",
        side=candidate.side,
        classification=classification,
        nxt_actionable=_is_nxt_actionable(classification),
        summary=_build_summary(classification, recon.decision_support),
        reasons=tuple(list(recon.reasons) + extra_reasons),
        warnings=tuple(warnings),
        decision_support=recon.decision_support,
    )


def classify_nxt_holding(
    holding: NxtHoldingInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    reasons: list[str] = []
    warnings: list[str] = []
    decision_support = _holding_decision_support(context)

    if holding.quantity is None or holding.quantity <= 0:
        reasons.append("non_positive_quantity")
    if holding.currency and holding.currency.upper() != "KRW":
        reasons.append("currency_mismatch")
    if reasons:
        return NxtClassifierItem(
            item_id=holding.holding_id,
            symbol=holding.symbol,
            kind="holding",
            side=None,
            classification="data_mismatch_requires_review",
            nxt_actionable=False,
            summary=_build_summary("data_mismatch_requires_review", decision_support),
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if context.kr_universe is None:
        warnings.append("missing_kr_universe")
        classification: NxtClassification = "holding_watch_only"
    elif not context.kr_universe.nxt_eligible:
        warnings.append("non_nxt_venue")
        classification = "non_nxt_pending_ignore_for_nxt"
    else:
        classification = "holding_watch_only"

    _apply_orderbook_warnings(decision_support, context.orderbook, nxt_cfg, warnings)

    return NxtClassifierItem(
        item_id=holding.holding_id,
        symbol=holding.symbol,
        kind="holding",
        side=None,
        classification=classification,
        nxt_actionable=False,
        summary=_build_summary(classification, decision_support),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        decision_support=decision_support,
    )


__all__ = [
    "NxtClassification",
    "NxtKind",
    "NxtCandidateInput",
    "NxtHoldingInput",
    "NxtClassifierConfig",
    "NxtClassifierItem",
    "classify_nxt_pending_order",
    "classify_nxt_candidate",
    "classify_nxt_holding",
]
