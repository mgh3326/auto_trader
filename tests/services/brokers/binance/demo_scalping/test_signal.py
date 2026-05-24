"""ROB-307 PR1 — tests for the deterministic trend micro-breakout signal.

Strategy (operator-selected): ENTER LONG when sma_fast > sma_slow AND the
current close breaks above the prior N-bar high. Futures may take the
mirror SHORT (downtrend + breakdown); spot is long-only so shorts are
suppressed. Exits are fixed-bps TP/SL. The evaluator is pure over a
candle sequence — no network, no broker, fully deterministic.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import ReasonCode
from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    evaluate_signal,
)


def _mk(
    closes: list[str],
    *,
    highs: list[str] | None = None,
    lows: list[str] | None = None,
) -> list[Candle]:
    candles: list[Candle] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        high = Decimal(highs[i]) if highs else close
        low = Decimal(lows[i]) if lows else close
        candles.append(
            Candle(
                open_time_ms=1_000 + i * 60_000,
                open=close,
                high=high,
                low=low,
                close=close,
                close_time_ms=1_000 + i * 60_000 + 59_999,
            )
        )
    return candles


def _ramp(start: int, n: int, step: int) -> list[str]:
    return [str(start + step * i) for i in range(n)]


def test_insufficient_history_returns_no_entry() -> None:
    candles = _mk(_ramp(100, 5, 1))  # far fewer than sma_slow
    decision = evaluate_signal(candles, SignalConfig())
    assert decision.has_entry is False
    assert decision.side is None
    assert ReasonCode.INSUFFICIENT_HISTORY in decision.reason_codes


def test_uptrend_breakout_enters_long() -> None:
    candles = _mk(_ramp(100, 30, 1))  # monotonic up: fast>slow, close breaks prior high
    decision = evaluate_signal(candles, SignalConfig(allow_short=False))
    assert decision.has_entry is True
    assert decision.side == "BUY"
    assert ReasonCode.ENTER_LONG_BREAKOUT in decision.reason_codes
    assert decision.entry_price == candles[-1].close


def test_long_tp_sl_use_configured_bps() -> None:
    candles = _mk(_ramp(100, 30, 1))
    cfg = SignalConfig(tp_bps=Decimal("30"), sl_bps=Decimal("20"))
    decision = evaluate_signal(candles, cfg)
    entry = decision.entry_price
    assert decision.tp_price == entry * (
        Decimal("1") + Decimal("30") / Decimal("10000")
    )
    assert decision.sl_price == entry * (
        Decimal("1") - Decimal("20") / Decimal("10000")
    )
    assert decision.tp_price > entry > decision.sl_price


def test_uptrend_without_breakout_is_no_signal() -> None:
    closes = _ramp(100, 29, 1)  # 100..128
    closes.append("120")  # last close dips below the prior 20-bar high (128)
    candles = _mk(closes)
    decision = evaluate_signal(candles, SignalConfig())
    assert decision.has_entry is False
    assert ReasonCode.NO_SIGNAL in decision.reason_codes


def test_downtrend_breakdown_suppressed_when_short_disallowed() -> None:
    candles = _mk(_ramp(200, 30, -1))  # monotonic down
    decision = evaluate_signal(candles, SignalConfig(allow_short=False))
    assert decision.has_entry is False
    assert ReasonCode.NO_SIGNAL in decision.reason_codes


def test_downtrend_breakdown_enters_short_when_allowed() -> None:
    candles = _mk(_ramp(200, 30, -1))
    cfg = SignalConfig(allow_short=True, tp_bps=Decimal("30"), sl_bps=Decimal("20"))
    decision = evaluate_signal(candles, cfg)
    assert decision.has_entry is True
    assert decision.side == "SELL"
    assert ReasonCode.ENTER_SHORT_BREAKDOWN in decision.reason_codes
    entry = decision.entry_price
    # Short: TP below entry, SL above entry.
    assert decision.tp_price == entry * (
        Decimal("1") - Decimal("30") / Decimal("10000")
    )
    assert decision.sl_price == entry * (
        Decimal("1") + Decimal("20") / Decimal("10000")
    )
    assert decision.tp_price < entry < decision.sl_price


def test_confidence_is_bounded_unit_interval() -> None:
    candles = _mk(_ramp(100, 30, 1))
    decision = evaluate_signal(candles, SignalConfig())
    assert Decimal("0") <= decision.confidence <= Decimal("1")


def test_no_entry_confidence_is_zero() -> None:
    candles = _mk(_ramp(200, 30, -1))  # downtrend, short disallowed -> no entry
    decision = evaluate_signal(candles, SignalConfig(allow_short=False))
    assert decision.confidence == Decimal("0")


def test_signal_is_deterministic() -> None:
    candles = _mk(_ramp(100, 30, 1))
    cfg = SignalConfig()
    first = evaluate_signal(candles, cfg)
    second = evaluate_signal(candles, cfg)
    assert first == second
