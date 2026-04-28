"""Unit tests for app.services.nxt_classifier_service.

These tests cover only the pure NXT classifier + summary logic. They do
not import any broker, DB, Redis, or HTTP module; the service module
under test must not transitively import them either (see
test_nxt_classifier_service_safety.py).
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from app.services.nxt_classifier_service import (
    NxtCandidateInput,
    NxtClassifierConfig,
    NxtClassifierItem,
    NxtHoldingInput,
    classify_nxt_candidate,
    classify_nxt_holding,
    classify_nxt_pending_order,
)
from app.services.pending_reconciliation_service import (
    KrUniverseContext,
    MarketContextInput,
    OrderbookContext,
    OrderbookLevelContext,
    PendingOrderInput,
    QuoteContext,
    SupportResistanceContext,
    SupportResistanceLevel,
)


@pytest.mark.unit
def test_module_exposes_public_api() -> None:
    from app.services import nxt_classifier_service as svc

    assert hasattr(svc, "NxtCandidateInput")
    assert hasattr(svc, "NxtHoldingInput")
    assert hasattr(svc, "NxtClassifierConfig")
    assert hasattr(svc, "NxtClassifierItem")
    assert callable(svc.classify_nxt_pending_order)
    assert callable(svc.classify_nxt_candidate)
    assert callable(svc.classify_nxt_holding)


@pytest.mark.unit
def test_item_has_required_fields() -> None:
    expected = {
        "item_id",
        "symbol",
        "kind",
        "side",
        "classification",
        "nxt_actionable",
        "summary",
        "reasons",
        "warnings",
        "decision_support",
    }
    assert {f.name for f in fields(NxtClassifierItem)} == expected


def _order(**overrides) -> PendingOrderInput:
    base = {
        "order_id": "O1",
        "symbol": "005930",
        "market": "kr",
        "side": "buy",
        "ordered_price": Decimal("70000"),
        "ordered_qty": Decimal("10"),
        "remaining_qty": Decimal("10"),
        "currency": "KRW",
        "ordered_at": None,
    }
    base.update(overrides)
    return PendingOrderInput(**base)


def _ctx(
    *,
    quote: str | None = None,
    nxt_eligible: bool | None = True,
) -> MarketContextInput:
    kr = None if nxt_eligible is None else KrUniverseContext(nxt_eligible=nxt_eligible)
    return MarketContextInput(
        quote=(QuoteContext(price=Decimal(quote), as_of=None) if quote else None),
        orderbook=None,
        support_resistance=None,
        kr_universe=kr,
    )


def _ctx_with_sr(
    *,
    quote: str,
    support_price: str | None,
    resistance_price: str | None,
    nxt_eligible: bool = True,
) -> MarketContextInput:
    sr = SupportResistanceContext(
        nearest_support=(
            SupportResistanceLevel(
                price=Decimal(support_price), distance_pct=Decimal("0.5")
            )
            if support_price
            else None
        ),
        nearest_resistance=(
            SupportResistanceLevel(
                price=Decimal(resistance_price), distance_pct=Decimal("0.5")
            )
            if resistance_price
            else None
        ),
    )
    return MarketContextInput(
        quote=QuoteContext(price=Decimal(quote), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=nxt_eligible),
    )


def _ob(
    *,
    bid_price: str,
    ask_price: str,
    bid_qty: str = "100",
    ask_qty: str = "100",
    total_bid_qty: str | None = None,
    total_ask_qty: str | None = None,
) -> OrderbookContext:
    return OrderbookContext(
        best_bid=OrderbookLevelContext(
            price=Decimal(bid_price), quantity=Decimal(bid_qty)
        ),
        best_ask=OrderbookLevelContext(
            price=Decimal(ask_price), quantity=Decimal(ask_qty)
        ),
        total_bid_qty=Decimal(total_bid_qty) if total_bid_qty is not None else None,
        total_ask_qty=Decimal(total_ask_qty) if total_ask_qty is not None else None,
    )


def _candidate(**overrides) -> NxtCandidateInput:
    base = {
        "candidate_id": "C1",
        "symbol": "005930",
        "side": "buy",
        "proposed_price": Decimal("70000"),
        "proposed_qty": Decimal("10"),
        "currency": "KRW",
    }
    base.update(overrides)
    return NxtCandidateInput(**base)


def _holding(**overrides) -> NxtHoldingInput:
    base = {
        "holding_id": "H1",
        "symbol": "005930",
        "quantity": Decimal("10"),
        "currency": "KRW",
    }
    base.update(overrides)
    return NxtHoldingInput(**base)


# Task 3: Pending order error / non-NXT / unknown / too-far paths


@pytest.mark.unit
def test_pending_unknown_venue_maps_to_data_mismatch() -> None:
    item = classify_nxt_pending_order(_order(market="paper"), _ctx())
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert item.kind == "pending_order"


@pytest.mark.unit
def test_pending_data_mismatch_currency_maps_to_data_mismatch() -> None:
    item = classify_nxt_pending_order(_order(currency="USD"), _ctx())
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "currency_mismatch" in item.reasons


@pytest.mark.unit
def test_pending_non_nxt_kr_maps_to_non_nxt_pending_ignore() -> None:
    item = classify_nxt_pending_order(
        _order(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_pending_unknown_when_quote_missing() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(quote=None, nxt_eligible=True))
    assert item.classification == "unknown"
    assert item.nxt_actionable is False
    assert "missing_quote" in item.warnings


@pytest.mark.unit
def test_buy_pending_too_far_when_market_through_limit() -> None:
    # ordered 70000, current 80000 -> reconciliation says "too_far".
    item = classify_nxt_pending_order(_order(), _ctx(quote="80000"))
    assert item.classification == "buy_pending_too_far"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_sell_pending_too_optimistic_when_market_through_limit() -> None:
    # sell 70000, current 60000 -> reconciliation says "too_far".
    item = classify_nxt_pending_order(_order(side="sell"), _ctx(quote="60000"))
    assert item.classification == "sell_pending_too_optimistic"
    assert item.nxt_actionable is False


# Task 4: at-support / near-resistance / actionable / chasing paths


@pytest.mark.unit
def test_buy_pending_at_support_when_order_price_within_near_support_pct() -> None:
    # buy 70000, current 70200 -> reconciliation: near_fill.
    # support 70300 -> |70000-70300|/70000 = 0.4286% <= 1.0% -> at_support.
    ctx = _ctx_with_sr(quote="70200", support_price="70300", resistance_price=None)
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"
    assert item.nxt_actionable is True
    assert "order_within_near_support_pct" in item.reasons


@pytest.mark.unit
def test_buy_pending_actionable_when_support_far() -> None:
    # buy 70000, current 70200 -> reconciliation: near_fill.
    # support 60000 -> |70000-60000|/70000 = 14.28% > 1.0% -> actionable.
    ctx = _ctx_with_sr(quote="70200", support_price="60000", resistance_price=None)
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_actionable"
    assert item.nxt_actionable is True


@pytest.mark.unit
def test_buy_pending_actionable_when_no_support_data() -> None:
    item = classify_nxt_pending_order(
        _order(),
        MarketContextInput(
            quote=QuoteContext(price=Decimal("70200"), as_of=None),
            orderbook=None,
            support_resistance=None,
            kr_universe=KrUniverseContext(nxt_eligible=True),
        ),
    )
    assert item.classification == "buy_pending_actionable"
    assert item.nxt_actionable is True
    assert "missing_support_resistance" in item.warnings


@pytest.mark.unit
def test_sell_pending_near_resistance_when_order_price_within_near_resistance_pct() -> (
    None
):
    # sell 70000, current 69800 -> reconciliation: near_fill (|gap|<=0.5%).
    # resistance 70300 -> 0.4286% <= 1.0% -> near_resistance.
    ctx = _ctx_with_sr(quote="69800", support_price=None, resistance_price="70300")
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_near_resistance"
    assert item.nxt_actionable is True
    assert "order_within_near_resistance_pct" in item.reasons


@pytest.mark.unit
def test_sell_pending_actionable_when_resistance_far() -> None:
    ctx = _ctx_with_sr(quote="69800", support_price=None, resistance_price="80000")
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_actionable"


@pytest.mark.unit
def test_buy_pending_at_support_in_maintain_band() -> None:
    # buy 70000, current 68000 -> reconciliation: maintain (|gap|=2.857%, not too_far, not chasing).
    # support 69500 -> |70000-69500|/70000 = 0.71% <= 1.0% -> at_support.
    ctx = _ctx_with_sr(quote="68000", support_price="69500", resistance_price=None)
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"


@pytest.mark.unit
def test_chasing_risk_buy_maps_to_buy_pending_too_far() -> None:
    # ROB-22 chasing_risk path: buy 70000, current 67000, resistance 67500 (distance_pct 0.5).
    sr = SupportResistanceContext(
        nearest_support=None,
        nearest_resistance=SupportResistanceLevel(
            price=Decimal("67500"), distance_pct=Decimal("0.5")
        ),
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("67000"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_too_far"
    assert item.nxt_actionable is False


# Task 5: Candidate classifier tests


@pytest.mark.unit
def test_candidate_buy_at_support() -> None:
    ctx = _ctx_with_sr(quote="70200", support_price="70300", resistance_price=None)
    item = classify_nxt_candidate(_candidate(), ctx)
    assert item.kind == "candidate"
    assert item.classification == "buy_pending_at_support"
    assert item.nxt_actionable is True


@pytest.mark.unit
def test_candidate_sell_too_optimistic() -> None:
    item = classify_nxt_candidate(_candidate(side="sell"), _ctx(quote="60000"))
    assert item.classification == "sell_pending_too_optimistic"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_non_nxt_excluded() -> None:
    item = classify_nxt_candidate(
        _candidate(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_data_mismatch_currency() -> None:
    item = classify_nxt_candidate(_candidate(currency="USD"), _ctx(quote="70200"))
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_unknown_when_quote_missing() -> None:
    item = classify_nxt_candidate(_candidate(), _ctx(nxt_eligible=True))
    assert item.classification == "unknown"
    assert "missing_quote" in item.warnings


@pytest.mark.unit
def test_candidate_with_no_proposed_qty_still_classifies() -> None:
    # Candidates may not specify a quantity. The adapter substitutes Decimal("1")
    # so the reconciliation service does not flag non_positive_remaining_qty.
    ctx = _ctx_with_sr(quote="70200", support_price="70300", resistance_price=None)
    item = classify_nxt_candidate(_candidate(proposed_qty=None), ctx)
    assert item.classification == "buy_pending_at_support"
    assert "non_positive_remaining_qty" not in item.reasons


# Task 6: Holding classifier tests


@pytest.mark.unit
def test_holding_watch_only_when_nxt_eligible() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=True))
    assert item.kind == "holding"
    assert item.classification == "holding_watch_only"
    assert item.nxt_actionable is False
    assert item.side is None


@pytest.mark.unit
def test_holding_non_nxt_excluded() -> None:
    item = classify_nxt_holding(_holding(symbol="034220"), _ctx(nxt_eligible=False))
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False
    assert "non_nxt_venue" in item.warnings


@pytest.mark.unit
def test_holding_missing_kr_universe_falls_back_to_watch_only_with_warning() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=None))
    assert item.classification == "holding_watch_only"
    assert "missing_kr_universe" in item.warnings


@pytest.mark.unit
def test_holding_data_mismatch_non_positive_quantity() -> None:
    item = classify_nxt_holding(
        _holding(quantity=Decimal("0")), _ctx(nxt_eligible=True)
    )
    assert item.classification == "data_mismatch_requires_review"
    assert "non_positive_quantity" in item.reasons


@pytest.mark.unit
def test_holding_data_mismatch_currency() -> None:
    item = classify_nxt_holding(_holding(currency="USD"), _ctx(nxt_eligible=True))
    assert item.classification == "data_mismatch_requires_review"
    assert "currency_mismatch" in item.reasons


# Task 7: Spread / liquidity warnings


@pytest.mark.unit
def test_wide_spread_warning_emitted_above_threshold() -> None:
    # bid 69500, ask 70500 -> spread = 1000 / 70000 = 1.4286% > 1.0% (default threshold).
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69500", ask_price="70500"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "wide_spread" in item.warnings


@pytest.mark.unit
def test_wide_spread_not_emitted_below_threshold() -> None:
    # bid 69900, ask 70100 -> spread = 200 / 70000 ~= 0.286% < 1%.
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69900", ask_price="70100"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "wide_spread" not in item.warnings


@pytest.mark.unit
def test_thin_liquidity_warning_when_threshold_set() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(
            bid_price="70100",
            ask_price="70200",
            total_bid_qty="50",
            total_ask_qty="40",
        ),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(
        _order(),
        ctx,
        config=NxtClassifierConfig(thin_liquidity_total_qty=Decimal("200")),
    )
    assert "thin_liquidity" in item.warnings


@pytest.mark.unit
def test_thin_liquidity_warning_skipped_when_threshold_none() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(
            bid_price="70100",
            ask_price="70200",
            total_bid_qty="1",
            total_ask_qty="1",
        ),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "thin_liquidity" not in item.warnings


@pytest.mark.unit
def test_holding_emits_wide_spread_warning_too() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69500", ask_price="70500"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_holding(_holding(), ctx)
    assert "wide_spread" in item.warnings


# Task 8: Korean summary templates


@pytest.mark.unit
def test_summary_buy_pending_at_support_includes_support_price() -> None:
    ctx = _ctx_with_sr(quote="70200", support_price="70300", resistance_price=None)
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"
    assert "지지선" in item.summary
    assert "70300" in item.summary


@pytest.mark.unit
def test_summary_sell_pending_near_resistance_includes_resistance_price() -> None:
    ctx = _ctx_with_sr(quote="69800", support_price=None, resistance_price="70300")
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_near_resistance"
    assert "저항선" in item.summary
    assert "70300" in item.summary


@pytest.mark.unit
def test_summary_buy_pending_too_far_uses_review_template() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(quote="80000"))
    assert "재검토" in item.summary


@pytest.mark.unit
def test_summary_non_nxt_pending_uses_exclude_template() -> None:
    item = classify_nxt_pending_order(
        _order(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert "NXT" in item.summary
    assert "제외" in item.summary


@pytest.mark.unit
def test_summary_holding_watch_only_template() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=True))
    assert "보유" in item.summary
    assert "모니터링" in item.summary


@pytest.mark.unit
def test_summary_data_mismatch_template() -> None:
    item = classify_nxt_pending_order(_order(currency="USD"), _ctx())
    assert "데이터" in item.summary
    assert "검토" in item.summary


@pytest.mark.unit
def test_summary_unknown_template() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(nxt_eligible=True))
    assert "분류 불가" in item.summary


# ROB-29 fail-closed regression tests


@pytest.mark.unit
def test_pending_kr_missing_universe_fails_closed_to_data_mismatch() -> None:
    """ROB-29 fail-closed: KR pending without a kr_symbol_universe row must
    NEVER fall through to *_actionable / *_at_support. Default-to-actionable
    is a safety regression."""
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "missing_kr_universe" in item.warnings
    assert "missing_kr_universe_fail_closed" in item.reasons


@pytest.mark.unit
def test_pending_kr_missing_universe_overrides_at_support_attempt() -> None:
    """Even when S-R / quote / orderbook would otherwise yield a strong
    actionable signal, missing kr_universe must dominate."""
    sr = SupportResistanceContext(
        nearest_support=SupportResistanceLevel(
            price=Decimal("70300"), distance_pct=Decimal("0.5")
        ),
        nearest_resistance=None,
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=None,
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_kr_missing_universe_fails_closed() -> None:
    """ROB-29 fail-closed must apply to candidates as well as pending orders."""
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    item = classify_nxt_candidate(_candidate(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "missing_kr_universe_fail_closed" in item.reasons
