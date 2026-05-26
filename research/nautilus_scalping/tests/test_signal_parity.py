"""ROB-316 spike — correctness gates for the Nautilus signal port.

These prove the port is faithful and conservative:
* ``bar_to_candle`` preserves OHLC exactly;
* a rising series produces the same long-breakout decision as the production
  ``evaluate_signal`` (we literally call it — parity is by construction, and
  this pins the window + decimal handling);
* no decision before the buffer is warm (no-lookahead windowing);
* spot is long-only (short suppressed) while futures (``allow_short``) shorts a
  breakdown — the spot-vs-futures behavior required by ROB-316.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.objects import Price, Quantity
from signal_bridge import SignalState, bar_to_candle, required_bars

from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    evaluate_signal,
)

_BAR_TYPE = BarType.from_str("XRPUSDT.BINANCE-1-MINUTE-LAST-INTERNAL")


def _candle(close: str, *, high: str | None = None, low: str | None = None) -> Candle:
    c = Decimal(close)
    return Candle(
        open_time_ms=0,
        open=c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c - Decimal("0.05"),
        close=c,
        close_time_ms=60_000,
    )


def _rising_series(n: int = 25, step: str = "0.1", base: str = "100") -> list[Candle]:
    """Monotonically rising closes/highs → sma_fast > sma_slow and the last
    close breaks the prior high."""
    b, s = Decimal(base), Decimal(step)
    return [_candle(str(b + s * i)) for i in range(n)]


def _falling_series(n: int = 25, step: str = "0.1", base: str = "100") -> list[Candle]:
    b, s = Decimal(base), Decimal(step)
    return [_candle(str(b - s * i)) for i in range(n)]


def test_bar_to_candle_preserves_ohlc() -> None:
    bar = Bar(
        _BAR_TYPE,
        Price(1.4200, 4),
        Price(1.4300, 4),
        Price(1.4100, 4),
        Price(1.4250, 4),
        Quantity(100, 1),
        ts_event=180_000_000_000,
        ts_init=180_000_000_000,
    )
    candle = bar_to_candle(bar)
    assert candle.open == Decimal("1.42")
    assert candle.high == Decimal("1.43")
    assert candle.low == Decimal("1.41")
    assert candle.close == Decimal("1.425")
    # ts_event is the close time (ns -> ms); open is one interval earlier.
    assert candle.close_time_ms == 180_000
    assert candle.open_time_ms == 180_000 - 60_000


def test_long_breakout_triggers_buy() -> None:
    candles = _rising_series()
    cfg = SignalConfig()  # spot defaults, allow_short=False
    decision = evaluate_signal(candles, cfg)
    assert decision.has_entry
    assert decision.side == "BUY"
    entry = candles[-1].close
    assert decision.entry_price == entry
    assert decision.tp_price == entry * (Decimal("1") + cfg.tp_bps / Decimal("10000"))
    assert decision.sl_price == entry * (Decimal("1") - cfg.sl_bps / Decimal("10000"))


def test_signal_state_no_decision_until_warm() -> None:
    cfg = SignalConfig()
    state = SignalState(cfg)
    needed = required_bars(cfg)
    candles = _rising_series(needed)
    decisions = [state.update(c) for c in candles]
    # Only the final update (buffer warm) yields a decision.
    assert all(d is None for d in decisions[:-1])
    assert not state.warm or decisions[-1] is not None
    assert decisions[-1] is not None and decisions[-1].side == "BUY"


def test_spot_is_long_only() -> None:
    """Falling series + spot config (allow_short=False) -> no short entry."""
    decision = evaluate_signal(_falling_series(), SignalConfig(allow_short=False))
    assert not decision.has_entry
    assert decision.side is None


def test_futures_shorts_breakdown() -> None:
    """Same falling series + futures config (allow_short=True) -> SELL."""
    cfg = SignalConfig(allow_short=True)
    decision = evaluate_signal(_falling_series(), cfg)
    assert decision.has_entry
    assert decision.side == "SELL"
    entry = _falling_series()[-1].close
    assert decision.tp_price == entry * (Decimal("1") - cfg.tp_bps / Decimal("10000"))
    assert decision.sl_price == entry * (Decimal("1") + cfg.sl_bps / Decimal("10000"))
