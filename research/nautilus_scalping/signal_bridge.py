"""ROB-316 spike — bridge between Nautilus bars and the production signal.

The whole point of the spike is to backtest the **real** strategy, not a
reimplementation. So this module reuses
``app.services.brokers.binance.demo_scalping.signal.evaluate_signal``
verbatim; it only adapts Nautilus ``Bar`` objects into the ``Candle`` shape
the signal expects and maintains a rolling window of **closed** bars.

No-lookahead is structural: ``SignalState.update`` appends the just-closed
candle and only ever passes already-closed candles to ``evaluate_signal``.
"""

from __future__ import annotations

from collections import deque

from nautilus_trader.model.data import Bar

from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    SignalDecision,
    evaluate_signal,
)

_NS_PER_MS = 1_000_000
_MINUTE_MS = 60_000


def required_bars(config: SignalConfig) -> int:
    """Minimum closed bars before ``evaluate_signal`` is meaningful."""
    return max(config.sma_slow, config.breakout_lookback + 1)


def bar_to_candle(bar: Bar, *, interval_ms: int = _MINUTE_MS) -> Candle:
    """Adapt a Nautilus ``Bar`` to the production ``Candle`` (exact decimals)."""
    close_ms = bar.ts_event // _NS_PER_MS
    return Candle(
        open_time_ms=close_ms - interval_ms,
        open=bar.open.as_decimal(),
        high=bar.high.as_decimal(),
        low=bar.low.as_decimal(),
        close=bar.close.as_decimal(),
        close_time_ms=close_ms,
    )


class SignalState:
    """Rolling buffer of closed candles feeding ``evaluate_signal``."""

    def __init__(self, config: SignalConfig) -> None:
        self._config = config
        self._needed = required_bars(config)
        self._candles: deque[Candle] = deque(maxlen=self._needed)

    def update(self, candle: Candle) -> SignalDecision | None:
        """Append a closed candle; return a decision once warmed up."""
        self._candles.append(candle)
        if len(self._candles) < self._needed:
            return None
        return evaluate_signal(list(self._candles), self._config)

    @property
    def warm(self) -> bool:
        return len(self._candles) >= self._needed
