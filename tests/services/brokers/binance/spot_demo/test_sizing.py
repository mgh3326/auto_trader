"""ROB-298 — Spot Demo sizing helper: floor to LOT_SIZE, never round up."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.spot_demo.sizing import (
    SizingBlocked,
    SizingResult,
    compute_demo_order_qty,
)


def test_floor_to_step_size() -> None:
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.001"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    # target qty = 10/100 = 0.1; floor to 0.001 step = 0.100
    assert result.qty == Decimal("0.100")
    assert result.notional_usdt == Decimal("10.000")


def test_blocked_when_floor_below_min_notional() -> None:
    # price=$100, step=1.0, min_notional=$50, cap=$10
    # target qty = 10/100 = 0.1, floor to 1.0 step = 0 → blocked
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("100"),
        min_notional=Decimal("50"),
        step_size=Decimal("1.0"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingBlocked)
    assert "MIN_NOTIONAL" in result.reason


def test_never_rounds_up_past_cap() -> None:
    # target $10 cap, step=0.01, price=$3 → floor qty=3.33; notional=9.99 ≤ cap
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("10"),
        price=Decimal("3"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.01"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_target_above_cap_clipped_to_cap() -> None:
    result = compute_demo_order_qty(
        target_notional_usdt=Decimal("20"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.001"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, SizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_cap_must_be_positive() -> None:
    with pytest.raises(ValueError):
        compute_demo_order_qty(
            target_notional_usdt=Decimal("10"),
            price=Decimal("100"),
            min_notional=Decimal("5"),
            step_size=Decimal("0.001"),
            cap_usdt=Decimal("0"),
        )
