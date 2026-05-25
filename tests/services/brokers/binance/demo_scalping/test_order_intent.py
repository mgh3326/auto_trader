"""ROB-307 PR2 — tests for the order-intent contract (signal → executor).

``build_order_intent`` converts an entry ``SignalDecision`` (plus the
risk-approved notional cap) into an explicit, validated ``OrderIntent``
that the one-shot executor consumes. Pure: no broker, no DB, no network.
Carries reason codes + source timestamps for ledger/audit metadata.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import (
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import (
    OrderIntent,
    build_order_intent,
)
from app.services.brokers.binance.demo_scalping.signal import SignalDecision


def _entry_signal(side: str = "BUY") -> SignalDecision:
    long = side == "BUY"
    entry = Decimal("100")
    return SignalDecision(
        has_entry=True,
        side=side,
        entry_price=entry,
        tp_price=entry * (Decimal("1.003") if long else Decimal("0.997")),
        sl_price=entry * (Decimal("0.998") if long else Decimal("1.002")),
        confidence=Decimal("0.5"),
        reason_codes=(
            ReasonCode.ENTER_LONG_BREAKOUT
            if long
            else ReasonCode.ENTER_SHORT_BREAKDOWN,
        ),
    )


def _no_entry() -> SignalDecision:
    return SignalDecision(
        has_entry=False,
        side=None,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=(ReasonCode.NO_SIGNAL,),
    )


def test_build_from_entry_signal_produces_intent() -> None:
    intent = build_order_intent(
        _entry_signal("BUY"),
        product="spot",
        symbol="XRPUSDT",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=1_779_000_000_000,
        evaluated_at_ms=1_779_000_001_000,
    )
    assert isinstance(intent, OrderIntent)
    assert intent.product == "spot"
    assert intent.symbol == "XRPUSDT"
    assert intent.side == "BUY"
    assert intent.order_type == "MARKET"
    assert intent.target_notional_usdt == Decimal("10")  # the risk cap
    assert intent.tp_price is not None and intent.sl_price is not None
    assert ReasonCode.ENTER_LONG_BREAKOUT in intent.reason_codes
    assert intent.source_candle_close_time_ms == 1_779_000_000_000


def test_no_entry_signal_yields_none() -> None:
    intent = build_order_intent(
        _no_entry(),
        product="spot",
        symbol="XRPUSDT",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )
    assert intent is None


def test_target_notional_respects_cap() -> None:
    limits = ScalpingRiskLimits(max_notional_usdt=Decimal("7"))
    intent = build_order_intent(
        _entry_signal("BUY"),
        product="usdm_futures",
        symbol="SOLUSDT",
        limits=limits,
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )
    assert intent is not None
    assert intent.target_notional_usdt == Decimal("7")


def test_futures_short_intent_preserved() -> None:
    intent = build_order_intent(
        _entry_signal("SELL"),
        product="usdm_futures",
        symbol="XRPUSDT",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )
    assert intent is not None
    assert intent.side == "SELL"
    assert ReasonCode.ENTER_SHORT_BREAKDOWN in intent.reason_codes


def test_intent_evidence_dict_is_json_safe() -> None:
    import json

    intent = build_order_intent(
        _entry_signal("BUY"),
        product="spot",
        symbol="XRPUSDT",
        limits=ScalpingRiskLimits(),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )
    assert intent is not None
    payload = intent.to_evidence_dict()
    assert payload["product"] == "spot"
    assert payload["target_notional_usdt"] == "10"
    json.dumps(payload)  # must not raise
