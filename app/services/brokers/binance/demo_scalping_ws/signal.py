"""ROB-317 — event-driven scalping signal.

Wraps the pure, candle-based ``demo_scalping.signal.evaluate_signal`` with a
bounded rolling buffer. On each closed kline the buffer is appended and the
signal re-evaluated — reacting at candle close rather than on a 5-minute
timer. The strategy/thresholds are reused verbatim; only the feed cadence
changes. See ROB-317 design §1, §3.2.
"""

from __future__ import annotations

from collections import deque

from app.services.brokers.binance.demo_scalping.contract import Product
from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    SignalDecision,
    evaluate_signal,
)
from app.services.brokers.binance.demo_scalping_ws.events import kline_to_candle
from app.services.brokers.binance.ws_client import KlineEvent


class EventDrivenSignal:
    """Per-symbol rolling-buffer adapter over ``evaluate_signal``."""

    def __init__(
        self,
        *,
        product: Product,
        symbol: str,
        config: SignalConfig | None = None,
        max_candles: int = 200,
    ) -> None:
        self._product = product
        self._symbol = symbol
        # Spot is long-only; futures may take the mirror short (same default
        # as demo_scalping.runner.evaluate_symbol).
        self._config = config or SignalConfig(allow_short=product == "usdm_futures")
        self._candles: deque[Candle] = deque(maxlen=max_candles)

    @property
    def candle_count(self) -> int:
        return len(self._candles)

    def ingest_kline(self, event: KlineEvent) -> SignalDecision:
        """Append the closed candle and re-evaluate the signal."""
        self._candles.append(kline_to_candle(event))
        return evaluate_signal(list(self._candles), self._config)
