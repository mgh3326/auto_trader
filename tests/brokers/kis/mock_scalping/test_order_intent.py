"""KIS mock scalping order-intent tests (ROB-321 PR3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.contract import ScalpingRiskLimits
from app.services.brokers.kis.mock_scalping.order_intent import build_order_intent
from app.services.brokers.kis.mock_scalping.signal import SignalDecision


def _buy_signal() -> SignalDecision:
    return SignalDecision(
        has_entry=True,
        side="BUY",
        entry_price=Decimal("70000"),
        tp_price=Decimal("70210"),
        sl_price=Decimal("69860"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
    )


@pytest.mark.unit
def test_build_from_buy_signal_pins_notional_cap() -> None:
    intent = build_order_intent(
        _buy_signal(),
        symbol="005930",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=111,
        evaluated_at_ms=222,
    )
    assert intent is not None
    assert intent.symbol == "005930"
    assert intent.side == "BUY"
    assert intent.account_mode == "kis_mock"
    assert intent.target_notional_krw == ScalpingRiskLimits().max_notional_krw
    assert intent.entry_reference_price == Decimal("70000")
    assert intent.source_candle_close_time_ms == 111


@pytest.mark.unit
def test_no_entry_signal_yields_none() -> None:
    no_entry = SignalDecision(
        has_entry=False,
        side=None,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=("no_signal",),
    )
    assert (
        build_order_intent(
            no_entry,
            symbol="005930",
            limits=ScalpingRiskLimits(),
            source_candle_close_time_ms=1,
            evaluated_at_ms=2,
        )
        is None
    )


@pytest.mark.unit
def test_sell_signal_yields_none_long_only() -> None:
    sell = SignalDecision(
        has_entry=True,
        side="SELL",
        entry_price=Decimal("70000"),
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0.5"),
        reason_codes=(),
    )
    assert (
        build_order_intent(
            sell,
            symbol="005930",
            limits=ScalpingRiskLimits(),
            source_candle_close_time_ms=1,
            evaluated_at_ms=2,
        )
        is None
    )


@pytest.mark.unit
def test_evidence_dict_is_json_safe() -> None:
    intent = build_order_intent(
        _buy_signal(),
        symbol="005930",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=111,
        evaluated_at_ms=222,
    )
    assert intent is not None
    ev = intent.to_evidence_dict()
    assert ev["target_notional_krw"] == "100000"
    assert ev["entry_reference_price"] == "70000"
    assert ev["account_mode"] == "kis_mock"
    assert ev["reason_codes"] == ["enter_long_breakout"]
