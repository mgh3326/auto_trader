"""ROB-321 PR3 — KIS mock scalping daemon supervisor (read-only, dry-run).

Consumes an injectable event source of parsed quote frames (real
``KISQuoteWebSocket`` in production via a thin adapter; a fake async iterator in
tests). Orderbook snapshots refresh per-symbol bid/ask state; trade ticks feed a
1-minute candle aggregator. When a candle closes, the long-only signal is
re-evaluated; an entry emits a ``TriggerEvent`` only when the symbol has a fresh
orderbook quote (within the freshness window) AND the per-symbol debounce window
has elapsed.

The supervisor performs **NO risk re-check, NO order, NO ledger write** —
``on_trigger`` is a caller-supplied coroutine. PR4 wires the confirm-gated mock
executor (which owns the ledger risk re-check). Import-guarded: this package may
not import any order/ledger/execution module.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from app.services.brokers.kis.mock_scalping.contract import ReasonCode, Side
from app.services.brokers.kis.mock_scalping.signal import (
    SignalConfig,
    SignalDecision,
    evaluate_signal,
)
from app.services.brokers.kis.mock_scalping_ws.candles import CandleAggregator
from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.brokers.kis.mock_scalping_ws.state import MarketState

logger = logging.getLogger("rob321.kis_mock_scalping_ws")

QuoteEvent = QuoteTick | OrderBookSnapshot
SourceFactory = Callable[[], AsyncIterator[QuoteEvent]]
OnTrigger = Callable[["TriggerEvent"], Awaitable[None]]
Clock = Callable[[], float]
StopWhen = Callable[[], bool]

_CANDLE_BUFFER = 200


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """An event-driven entry candidate, pre-risk-check (PR4 consumes it)."""

    symbol: str
    side: Side
    decision: SignalDecision
    source_candle_close_time_ms: int
    bid: float | None
    ask: float | None
    spread_bps: float | None
    data_age_seconds: float | None
    emitted_at: float
    account_mode: str = "kis_mock"


class KisScalpingSupervisor:
    """Per-symbol quote router with candle aggregation + freshness/debounce gates."""

    def __init__(
        self,
        *,
        symbols: list[str],
        signal_config: SignalConfig | None = None,
        max_data_age_seconds: float = 60.0,
        debounce_seconds: float = 300.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._config = signal_config or SignalConfig()
        self._max_data_age_seconds = max_data_age_seconds
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._state: dict[str, MarketState] = {
            s: MarketState(symbol=s) for s in symbols
        }
        self._aggregators: dict[str, CandleAggregator] = {
            s: CandleAggregator() for s in symbols
        }
        self._candles: dict[str, deque] = {
            s: deque(maxlen=_CANDLE_BUFFER) for s in symbols
        }
        self._last_trigger_at: dict[str, float] = {}

    def market_state(self, symbol: str) -> MarketState | None:
        """Current per-symbol state (the broker adapter reads bid/ask/last)."""
        return self._state.get(symbol)

    async def run(
        self,
        source_factory: SourceFactory,
        *,
        on_trigger: OnTrigger,
        stop_after: int | None = None,
        stop_when: StopWhen | None = None,
    ) -> None:
        """Consume one source to exhaustion (single connection pass)."""
        consumed = 0
        source = source_factory()
        async for event in source:
            trigger = self._handle_event(event)
            if trigger is not None:
                await on_trigger(trigger)
                if stop_when is not None and stop_when():
                    return
            consumed += 1
            if stop_after is not None and consumed >= stop_after:
                return

    def _handle_event(self, event: QuoteEvent) -> TriggerEvent | None:
        now = self._clock()
        state = self._state.get(event.symbol)
        if state is None:
            return None  # not a subscribed/allowlisted symbol

        if isinstance(event, OrderBookSnapshot):
            state.update_from_book(event, now=now)
            return None

        # QuoteTick → update last + feed candle aggregator
        state.update_from_tick(event, now=now)
        closed = self._aggregators[event.symbol].add(
            Decimal(str(event.last_price)), now=now
        )
        if closed is None:
            return None

        buffer = self._candles[event.symbol]
        buffer.append(closed)
        decision = evaluate_signal(list(buffer), self._config)
        if not decision.has_entry or decision.side is None:
            return None
        return self._gate_and_build(
            event.symbol, state, decision, closed.close_time_ms, now
        )

    def _gate_and_build(
        self,
        symbol: str,
        state: MarketState,
        decision: SignalDecision,
        source_candle_close_time_ms: int,
        now: float,
    ) -> TriggerEvent | None:
        # Execution requires a fresh live quote: a recent orderbook with both
        # sides present. Trade-tick freshness alone does not qualify (the spread
        # guard would otherwise pass on a dead book).
        book_age = state.book_age_seconds(now=now)
        if (
            state.bid is None
            or state.ask is None
            or book_age is None
            or book_age > self._max_data_age_seconds
        ):
            logger.info(
                "trigger suppressed symbol=%s reason=%s", symbol, ReasonCode.STALE_DATA
            )
            return None

        last = self._last_trigger_at.get(symbol)
        if last is not None and (now - last) < self._debounce_seconds:
            logger.info("trigger suppressed symbol=%s reason=debounce", symbol)
            return None

        self._last_trigger_at[symbol] = now
        logger.info(
            "trigger symbol=%s side=%s reasons=%s",
            symbol,
            decision.side,
            decision.reason_codes,
        )
        return TriggerEvent(
            symbol=symbol,
            side=decision.side,
            decision=decision,
            source_candle_close_time_ms=source_candle_close_time_ms,
            bid=state.bid,
            ask=state.ask,
            spread_bps=state.spread_bps(),
            data_age_seconds=book_age,
            emitted_at=now,
        )
