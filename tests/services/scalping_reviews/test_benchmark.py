from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.scalping_reviews.benchmark import (
    daily_buy_and_hold_return_bps,
    notional_weighted_benchmark_bps,
)


def test_daily_buy_and_hold_return_bps_up_down_flat() -> None:
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("101")
    ) == Decimal("100")
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("99")
    ) == Decimal("-100")
    assert daily_buy_and_hold_return_bps(
        open_price=Decimal("100"), close_price=Decimal("100")
    ) == Decimal("0")


def test_daily_buy_and_hold_rejects_nonpositive_open() -> None:
    with pytest.raises(ValueError):
        daily_buy_and_hold_return_bps(open_price=Decimal("0"), close_price=Decimal("1"))


def test_notional_weighted_benchmark_bps_weights_by_notional() -> None:
    # (100*100 + 300*-20) / 400 = 10
    assert notional_weighted_benchmark_bps(
        [(Decimal("100"), Decimal("100")), (Decimal("300"), Decimal("-20"))]
    ) == Decimal("10")


def test_notional_weighted_benchmark_bps_none_when_no_notional() -> None:
    assert notional_weighted_benchmark_bps([]) is None
    assert notional_weighted_benchmark_bps([(Decimal("0"), Decimal("100"))]) is None
