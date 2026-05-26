"""KIS mock scalping signal tests (ROB-321 PR3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.contract import ReasonCode
from app.services.brokers.kis.mock_scalping.signal import (
    Candle,
    SignalConfig,
    evaluate_signal,
)


def _candle(
    i: int, close: int, high: int | None = None, low: int | None = None
) -> Candle:
    return Candle(
        open_time_ms=i * 60_000,
        open=Decimal(close),
        high=Decimal(high if high is not None else close),
        low=Decimal(low if low is not None else close),
        close=Decimal(close),
        close_time_ms=i * 60_000 + 59_999,
    )


def _uptrend(n: int = 30, start: int = 1000, step: int = 5) -> list[Candle]:
    return [_candle(i, start + i * step) for i in range(n)]


@pytest.mark.unit
def test_insufficient_history_returns_no_entry() -> None:
    decision = evaluate_signal(_uptrend(n=10), SignalConfig())
    assert decision.has_entry is False
    assert decision.reason_codes == (ReasonCode.INSUFFICIENT_HISTORY,)


@pytest.mark.unit
def test_downtrend_returns_no_signal() -> None:
    candles = [_candle(i, 2000 - i * 5) for i in range(30)]  # descending
    decision = evaluate_signal(candles, SignalConfig())
    assert decision.has_entry is False
    assert decision.reason_codes == (ReasonCode.NO_SIGNAL,)


@pytest.mark.unit
def test_uptrend_breakout_enters_long_with_tp_sl() -> None:
    decision = evaluate_signal(_uptrend(), SignalConfig())
    assert decision.has_entry is True
    assert decision.side == "BUY"
    assert decision.reason_codes == (ReasonCode.ENTER_LONG_BREAKOUT,)
    entry = decision.entry_price
    assert entry == Decimal(1000 + 29 * 5)
    assert decision.tp_price == entry * (
        Decimal("1") + Decimal("30") / Decimal("10000")
    )
    assert decision.sl_price == entry * (
        Decimal("1") - Decimal("20") / Decimal("10000")
    )
    assert Decimal("0") <= decision.confidence <= Decimal("1")


@pytest.mark.unit
def test_no_chase_guard_rejects_spike() -> None:
    candles = _uptrend()
    # Replace the last candle's close with a far breakout above the prior high.
    prior_high = max(c.high for c in candles[:-1][-20:])
    spike = int(prior_high * Decimal("1.02"))  # ~200 bps > max_chase_bps 50
    candles[-1] = _candle(len(candles) - 1, spike)
    decision = evaluate_signal(candles, SignalConfig())
    assert decision.has_entry is False
    assert decision.reason_codes == (ReasonCode.CHASE_TOO_FAR,)


@pytest.mark.unit
def test_no_short_branch_even_on_downtrend_breakdown() -> None:
    # Strong downtrend breaking below prior low must NOT produce a SELL entry.
    candles = [
        _candle(i, 2000 - i * 5, high=2000 - i * 5, low=2000 - i * 5) for i in range(30)
    ]
    decision = evaluate_signal(candles, SignalConfig())
    assert decision.side is None
    assert decision.has_entry is False
