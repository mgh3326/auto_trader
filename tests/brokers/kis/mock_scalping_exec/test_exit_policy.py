"""Pure exit-policy tests (ROB-321 PR4a)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_exec.exit_policy import (
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_STOP,
    decide_exit,
)

_TP = Decimal("70210")
_SL = Decimal("69860")


def _decide(bid, *, last=None, elapsed=0.0, max_hold=120.0):
    return decide_exit(
        bid=bid,
        last_price=last,
        tp_price=_TP,
        sl_price=_SL,
        elapsed_seconds=elapsed,
        max_hold_seconds=max_hold,
    )


@pytest.mark.unit
def test_take_profit_on_bid_at_or_above_tp() -> None:
    assert _decide(Decimal("70210")) == TAKE_PROFIT
    assert _decide(Decimal("70300")) == TAKE_PROFIT


@pytest.mark.unit
def test_stop_loss_on_bid_at_or_below_sl() -> None:
    assert _decide(Decimal("69860")) == STOP_LOSS
    assert _decide(Decimal("69000")) == STOP_LOSS


@pytest.mark.unit
def test_hold_between_sl_and_tp() -> None:
    assert _decide(Decimal("70000")) is None


@pytest.mark.unit
def test_time_stop_when_held_too_long() -> None:
    assert _decide(Decimal("70000"), elapsed=120.0, max_hold=120.0) == TIME_STOP


@pytest.mark.unit
def test_price_exit_takes_priority_over_time_stop() -> None:
    # both tp hit and over time -> take_profit (price priority)
    assert _decide(Decimal("70300"), elapsed=999.0) == TAKE_PROFIT


@pytest.mark.unit
def test_falls_back_to_last_price_when_no_bid() -> None:
    assert _decide(None, last=Decimal("70300")) == TAKE_PROFIT
    # no bid, no last, not timed out -> hold
    assert _decide(None, last=None) is None
