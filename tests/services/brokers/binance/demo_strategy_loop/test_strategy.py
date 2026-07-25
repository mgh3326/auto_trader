"""ROB-993 — plugin strategy interface."""

from __future__ import annotations

from app.services.brokers.binance.demo_strategy_loop.strategy import (
    NullStrategy,
    Signal,
)


def test_null_strategy_always_returns_none() -> None:
    strategy = NullStrategy()
    assert strategy.strategy_id == "null"
    assert strategy.evaluate({}, decision_ts=1_700_000_000_000) is None
    assert strategy.evaluate({"XRPUSDT": ()}, decision_ts=1_700_000_000_000) is None


def test_signal_is_frozen_dataclass() -> None:
    signal = Signal(
        symbol="xrpusdt",
        side="BUY",
        decision_ts=1_700_000_000_000,
        strategy_id="test",
        reason="unit test",
    )
    assert signal.symbol == "xrpusdt"
    assert signal.sl_price is None
    assert signal.confidence is None
    try:
        signal.side = "SELL"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("Signal must be frozen")
