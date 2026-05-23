"""ROB-299 — fee-aware close qty + residual dust classification."""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.spot_demo.sizing import (
    CloseQtyDust,
    CloseQtyResult,
    classify_close_residual,
    compute_close_qty,
)

_STEP = Decimal("0.1")
_MIN_NOTIONAL = Decimal("5")
_PRICE = Decimal("2.0")  # 1 unit = 2 USDT


def test_compute_close_qty_uses_free_balance_not_buy_qty():
    # Bought 6 units, but only 5.93 free after commission.
    res = compute_close_qty(
        free_balance=Decimal("5.93"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyResult)
    assert res.qty == Decimal("5.9")  # floored to step, NOT 6
    assert res.notional_usdt == Decimal("11.8")


def test_compute_close_qty_fee_reduced_free_balance_still_sellable():
    res = compute_close_qty(
        free_balance=Decimal("3.001"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyResult)
    assert res.qty == Decimal("3.0")
    assert res.notional_usdt == Decimal("6.0")


def test_compute_close_qty_residual_below_min_notional_is_dust():
    # 2.0 units * 2.0 = 4.0 USDT < min_notional 5.
    res = compute_close_qty(
        free_balance=Decimal("2.0"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyDust)
    assert res.free == Decimal("2.0")
    assert res.notional_usdt == Decimal("4.0")


def test_compute_close_qty_sub_step_free_is_dust():
    res = compute_close_qty(
        free_balance=Decimal("0.05"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
    )
    assert isinstance(res, CloseQtyDust)
    assert res.free == Decimal("0.05")


def test_classify_residual_below_min_notional_is_dust():
    outcome = classify_close_residual(
        free_after=Decimal("0.5"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=True,
    )
    assert outcome.kind == "dust"
    assert outcome.remediation_hint is None


def test_classify_residual_sellable_remainder_with_clean_book_is_anomaly():
    # 3.0 * 2.0 = 6.0 >= min_notional: a sellable chunk was left behind.
    outcome = classify_close_residual(
        free_after=Decimal("3.0"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=True,
    )
    assert outcome.kind == "anomaly"
    assert outcome.remediation_hint  # operator-readable, non-empty


def test_classify_residual_dirty_book_is_anomaly():
    outcome = classify_close_residual(
        free_after=Decimal("0.5"),
        price=_PRICE,
        min_notional=_MIN_NOTIONAL,
        step_size=_STEP,
        open_orders_empty=False,
    )
    assert outcome.kind == "anomaly"
    assert outcome.remediation_hint
