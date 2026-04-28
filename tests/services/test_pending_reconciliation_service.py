"""Unit tests for app.services.pending_reconciliation_service.

These tests cover only the pure classifier + warning logic. They do not
import any broker, DB, Redis, or HTTP module; the service module under
test must not transitively import them either (see
test_pending_reconciliation_service_safety.py).
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.pending_reconciliation_service import (
    KrUniverseContext,
    MarketContextInput,
    PendingOrderInput,
    QuoteContext,
    ReconciliationConfig,
    SupportResistanceContext,
    SupportResistanceLevel,
    reconcile_pending_order,
    reconcile_pending_orders,
)


@pytest.mark.unit
def test_module_exposes_public_api() -> None:
    from app.services import pending_reconciliation_service as svc

    assert hasattr(svc, "PendingOrderInput")
    assert hasattr(svc, "QuoteContext")
    assert hasattr(svc, "OrderbookContext")
    assert hasattr(svc, "OrderbookLevelContext")
    assert hasattr(svc, "SupportResistanceContext")
    assert hasattr(svc, "SupportResistanceLevel")
    assert hasattr(svc, "KrUniverseContext")
    assert hasattr(svc, "MarketContextInput")
    assert hasattr(svc, "ReconciliationConfig")
    assert hasattr(svc, "PendingReconciliationItem")
    assert callable(svc.reconcile_pending_order)
    assert callable(svc.reconcile_pending_orders)


@pytest.mark.unit
def test_item_has_required_fields() -> None:
    from app.services.pending_reconciliation_service import (
        PendingReconciliationItem,
    )

    expected = {
        "order_id",
        "symbol",
        "market",
        "side",
        "classification",
        "nxt_actionable",
        "gap_pct",
        "reasons",
        "warnings",
        "decision_support",
    }
    assert {f.name for f in fields(PendingReconciliationItem)} == expected


def _empty_context(kr_universe: KrUniverseContext | None = None) -> MarketContextInput:
    return MarketContextInput(
        quote=None,
        orderbook=None,
        support_resistance=None,
        kr_universe=kr_universe,
    )


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


@pytest.mark.unit
def test_unknown_venue_market() -> None:
    item = reconcile_pending_order(_order(market="paper"), _empty_context())
    assert item.classification == "unknown_venue"
    assert "unknown_venue" in item.warnings
    assert item.nxt_actionable is None


@pytest.mark.unit
def test_unknown_venue_side() -> None:
    item = reconcile_pending_order(_order(side="short"), _empty_context())
    assert item.classification == "unknown_venue"
    assert "unknown_side" in item.warnings


@pytest.mark.unit
def test_data_mismatch_currency_kr_usd() -> None:
    item = reconcile_pending_order(_order(currency="USD"), _empty_context())
    assert item.classification == "data_mismatch"
    assert "currency_mismatch" in item.reasons


@pytest.mark.unit
def test_data_mismatch_non_positive_price() -> None:
    item = reconcile_pending_order(_order(ordered_price=Decimal("0")), _empty_context())
    assert item.classification == "data_mismatch"
    assert "non_positive_ordered_price" in item.reasons


@pytest.mark.unit
def test_kr_pending_non_nxt() -> None:
    ctx = _empty_context(
        kr_universe=KrUniverseContext(
            nxt_eligible=False, name="LG디스플레이", exchange="KOSPI"
        )
    )
    item = reconcile_pending_order(_order(symbol="034220"), ctx)
    assert item.classification == "kr_pending_non_nxt"
    assert item.nxt_actionable is False
    assert "non_nxt_venue" in item.warnings


@pytest.mark.unit
def test_kr_universe_missing_warning() -> None:
    item = reconcile_pending_order(_order(symbol="034220"), _empty_context())
    assert "missing_kr_universe" in item.warnings
    assert item.nxt_actionable is None


@pytest.mark.unit
def test_kr_nxt_eligible_marks_nxt_actionable_true() -> None:
    ctx = _empty_context(kr_universe=KrUniverseContext(nxt_eligible=True))
    item = reconcile_pending_order(_order(), ctx)
    assert item.nxt_actionable is True


def _ctx_with_quote(
    price: str, *, as_of=None, kr_eligible: bool | None = True
) -> MarketContextInput:
    kr = None if kr_eligible is None else KrUniverseContext(nxt_eligible=kr_eligible)
    return MarketContextInput(
        quote=QuoteContext(price=Decimal(price), as_of=as_of),
        orderbook=None,
        support_resistance=None,
        kr_universe=kr,
    )


@pytest.mark.unit
def test_near_fill_buy() -> None:
    # ordered 70000, current 70200 → gap +0.2857%, |gap| <= 0.5
    item = reconcile_pending_order(_order(), _ctx_with_quote("70200"))
    assert item.classification == "near_fill"
    assert item.gap_pct is not None
    assert abs(item.gap_pct - Decimal("0.2857")) < Decimal("0.001")


@pytest.mark.unit
def test_too_far_buy_through_market() -> None:
    # buy at 70000 but market is 80000 → +14.28%, signed_distance_to_fill = -14.28
    item = reconcile_pending_order(_order(), _ctx_with_quote("80000"))
    assert item.classification == "too_far"
    assert "gap_against_fill_exceeds_too_far_pct" in item.reasons


@pytest.mark.unit
def test_too_far_sell_through_market() -> None:
    # sell at 70000 but market is 60000 → -14.28%, signed_distance_to_fill = -14.28
    item = reconcile_pending_order(_order(side="sell"), _ctx_with_quote("60000"))
    assert item.classification == "too_far"


@pytest.mark.unit
def test_maintain_default() -> None:
    # buy 70000, current 68000 → gap -2.857%, signed_distance_to_fill = +2.857%
    # |gap| > near_fill (0.5) and < chasing (3.0) → maintain
    item = reconcile_pending_order(_order(), _ctx_with_quote("68000"))
    assert item.classification == "maintain"


@pytest.mark.unit
def test_unknown_when_quote_missing() -> None:
    item = reconcile_pending_order(
        _order(), _empty_context(kr_universe=KrUniverseContext(nxt_eligible=True))
    )
    assert item.classification == "unknown"
    assert "missing_quote" in item.warnings
    assert item.gap_pct is None


@pytest.mark.unit
def test_stale_quote_warning_still_classifies() -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    stale_at = now - timedelta(seconds=600)
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=stale_at),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(), ctx, now=now)
    assert "stale_quote" in item.warnings
    assert item.classification == "near_fill"


@pytest.mark.unit
def test_decision_support_includes_gap_and_signed_distance() -> None:
    item = reconcile_pending_order(_order(), _ctx_with_quote("68000"))
    ds = item.decision_support
    assert ds["current_price"] == Decimal("68000")
    assert ds["gap_pct"] is not None
    assert ds["signed_distance_to_fill"] is not None


@pytest.mark.unit
def test_chasing_risk_buy_into_resistance() -> None:
    # buy at 70000, current 68000 → gap -2.857% (signed_distance_to_fill +2.857)
    # → does NOT exceed chasing_pct (3.0). Use a wider gap to qualify.
    # buy at 70000, current 67000 → gap -4.285% (signed_distance_to_fill +4.285)
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
    item = reconcile_pending_order(_order(), ctx)
    assert item.classification == "chasing_risk"
    assert "price_diverged_into_resistance" in item.reasons


@pytest.mark.unit
def test_chasing_risk_sell_into_support() -> None:
    sr = SupportResistanceContext(
        nearest_support=SupportResistanceLevel(
            price=Decimal("72500"), distance_pct=Decimal("0.5")
        ),
        nearest_resistance=None,
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("73000"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(side="sell"), ctx)
    assert item.classification == "chasing_risk"
    assert "price_diverged_into_support" in item.reasons


@pytest.mark.unit
def test_chasing_risk_skipped_when_sr_missing() -> None:
    # Same gap as above but no SR → falls back to maintain (gap is 4.28%, not too_far)
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("67000"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(), ctx)
    assert item.classification == "maintain"
    assert "missing_support_resistance" in item.warnings


@pytest.mark.unit
def test_reconcile_pending_orders_pairs_orders_and_contexts() -> None:
    o1 = _order(order_id="A", symbol="005930")
    o2 = _order(order_id="B", symbol="034220", currency="KRW")
    contexts = {
        "A": _ctx_with_quote("70200"),  # near_fill
        "B": MarketContextInput(
            quote=None,
            orderbook=None,
            support_resistance=None,
            kr_universe=KrUniverseContext(nxt_eligible=False),
        ),
    }
    items = reconcile_pending_orders([o1, o2], contexts)
    assert {item.order_id for item in items} == {"A", "B"}
    by_id = {item.order_id: item for item in items}
    assert by_id["A"].classification == "near_fill"
    assert by_id["B"].classification == "kr_pending_non_nxt"


@pytest.mark.unit
def test_reconcile_pending_orders_treats_missing_context_as_empty() -> None:
    o = _order(order_id="X")
    items = reconcile_pending_orders([o], {})
    assert len(items) == 1
    assert "missing_quote" in items[0].warnings


@pytest.mark.unit
def test_config_overrides_thresholds() -> None:
    # Default near_fill_pct is 0.5; with 5.0 override, 2.857% gap is near_fill.
    item = reconcile_pending_order(
        _order(),
        _ctx_with_quote("68000"),
        config=ReconciliationConfig(near_fill_pct=Decimal("5.0")),
    )
    assert item.classification == "near_fill"


@pytest.mark.unit
def test_two_callers_share_one_pure_service() -> None:
    """Demonstrate Research Run live refresh and Decision Session proposal
    generation can both call the service with their own context shapes
    without importing each other.
    """

    # 1. "Research Run live refresh" caller: builds context inline from
    # already-fetched quote + KR universe row.
    research_order = _order(order_id="research-1", symbol="005930")
    research_context = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    research_item = reconcile_pending_order(research_order, research_context)
    assert research_item.classification == "near_fill"

    # 2. "Decision Session proposal generation" caller: builds context from
    # SR + orderbook to drive proposal warnings.
    _order(order_id="proposal-1", symbol="034220", currency="KRW")
    proposal_context = MarketContextInput(
        quote=QuoteContext(price=Decimal("9800"), as_of=None),
        orderbook=None,
        support_resistance=SupportResistanceContext(
            nearest_support=None,
            nearest_resistance=SupportResistanceLevel(
                price=Decimal("9850"),
                distance_pct=Decimal("0.5"),
            ),
        ),
        kr_universe=KrUniverseContext(nxt_eligible=False),
    )
    proposal_item = reconcile_pending_order(
        PendingOrderInput(
            order_id="proposal-1",
            symbol="034220",
            market="kr",
            side="buy",
            ordered_price=Decimal("9500"),
            ordered_qty=Decimal("100"),
            remaining_qty=Decimal("100"),
            currency="KRW",
            ordered_at=None,
        ),
        proposal_context,
    )
    # Non-NXT KR pending always wins over chasing_risk (rule 3 fires before quote rules).
    assert proposal_item.classification == "kr_pending_non_nxt"
    assert proposal_item.nxt_actionable is False
    assert "non_nxt_venue" in proposal_item.warnings
