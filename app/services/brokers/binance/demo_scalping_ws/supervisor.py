"""ROB-317 — asyncio scalping daemon supervisor.

Consumes an injectable event source (real fstream client in production, a
fake async iterator in tests). Routes closed klines to the per-symbol signal;
on an entry, emits a TriggerEvent only when the symbol has a fresh bookTicker
quote (best bid/ask present and within the freshness window) and the
per-symbol debounce window has elapsed. bookTicker/aggTrade events refresh
state; aggTrade is momentum context only and never substitutes for a live
quote. ``run_with_reconnect`` backs off on stream drops.

The supervisor performs NO risk re-check and NO broker mutation: ``on_trigger``
is a caller-supplied coroutine (the WsExecutionBridge wires the confirm-gated
executor; the executor owns the ledger risk re-check). See ROB-317 design §6.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from websockets.exceptions import ConnectionClosed

from app.services.brokers.binance.demo_scalping.contract import (
    Product,
    ReasonCode,
    ScalpingRiskLimits,
    Side,
)
from app.services.brokers.binance.demo_scalping.signal import (
    SignalConfig,
    SignalDecision,
)
from app.services.brokers.binance.demo_scalping_ws.events import apply_event
from app.services.brokers.binance.demo_scalping_ws.market_stream import FuturesWsEvent
from app.services.brokers.binance.demo_scalping_ws.signal import EventDrivenSignal
from app.services.brokers.binance.demo_scalping_ws.state import MarketState
from app.services.brokers.binance.ws_client import (
    KlineEvent,
    compute_backoff_delay,
    is_unhealthy,
)

logger = logging.getLogger("rob317.demo_scalping_ws")

SourceFactory = Callable[[], AsyncIterator[FuturesWsEvent]]
OnTrigger = Callable[["TriggerEvent"], Awaitable[None]]
Clock = Callable[[], dt.datetime]
Sleep = Callable[[float], Awaitable[None]]
StopWhen = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """An event-driven entry candidate, pre-risk-check (slice 4 consumes it)."""

    product: Product
    symbol: str
    side: Side
    decision: SignalDecision
    source_candle_close_time_ms: int
    bid_price: Decimal | None
    ask_price: Decimal | None
    data_age_seconds: float | None
    emitted_at: dt.datetime


def _default_clock() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class ScalpingDaemonSupervisor:
    """Per-symbol event router with freshness + debounce gates."""

    def __init__(
        self,
        *,
        symbols: list[str],
        product: Product = "usdm_futures",
        limits: ScalpingRiskLimits | None = None,
        signal_config: SignalConfig | None = None,
        debounce_seconds: float = 300.0,
        clock: Clock = _default_clock,
    ) -> None:
        self._product = product
        self._limits = limits or ScalpingRiskLimits()
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._state: dict[str, MarketState] = {
            s: MarketState(symbol=s) for s in symbols
        }
        self._signals: dict[str, EventDrivenSignal] = {
            s: EventDrivenSignal(product=product, symbol=s, config=signal_config)
            for s in symbols
        }
        self._last_trigger_at: dict[str, dt.datetime] = {}

    async def run(
        self,
        source_factory: SourceFactory,
        *,
        on_trigger: OnTrigger,
        stop_after: int | None = None,
        stop_when: StopWhen | None = None,
    ) -> None:
        """Consume one source to exhaustion (single connection pass).

        ``stop_after`` bounds events consumed; ``stop_when`` is a predicate
        checked after each emitted trigger — bounded operator mode (e.g. a
        trigger-count cap) returns cleanly once it is true.
        """
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

    def _handle_event(self, event: FuturesWsEvent) -> TriggerEvent | None:
        now = self._clock()
        symbol = event.symbol
        state = self._state.get(symbol)
        if state is None:
            return None  # not an allowlisted/subscribed symbol
        if not isinstance(event, KlineEvent):
            apply_event(state, event)
            return None
        decision = self._signals[symbol].ingest_kline(event)
        if not decision.has_entry or decision.side is None:
            return None
        return self._gate_and_build(
            symbol,
            state,
            decision,
            now,
            source_candle_close_time_ms=int(event.close_time.timestamp() * 1000),
        )

    def _gate_and_build(
        self,
        symbol: str,
        state: MarketState,
        decision: SignalDecision,
        now: dt.datetime,
        *,
        source_candle_close_time_ms: int,
    ) -> TriggerEvent | None:
        # Execution requires a live quote: a fresh bookTicker with both sides
        # present. A fresh aggTrade alone does NOT qualify — without a current
        # best bid/ask the spread guard would receive spread=0 and pass, which
        # could place a confirmed order with no valid spread check. aggTrade
        # freshness stays momentum/trigger context only.
        book_age = state.book_data_age_seconds(now=now)
        if (
            state.bid_price is None
            or state.ask_price is None
            or book_age is None
            or book_age > self._limits.max_data_age_seconds
        ):
            logger.info(
                "trigger suppressed symbol=%s reason=%s",
                symbol,
                ReasonCode.STALE_DATA,
            )
            return None
        last = self._last_trigger_at.get(symbol)
        if last is not None and (now - last).total_seconds() < self._debounce_seconds:
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
            product=self._product,
            symbol=symbol,
            side=decision.side,
            decision=decision,
            source_candle_close_time_ms=source_candle_close_time_ms,
            bid_price=state.bid_price,
            ask_price=state.ask_price,
            # bookTicker age feeds the execution/spread guard, not the (possibly
            # newer) aggTrade timestamp.
            data_age_seconds=book_age,
            emitted_at=now,
        )

    async def run_with_reconnect(
        self,
        source_factory: SourceFactory,
        *,
        on_trigger: OnTrigger,
        sleep: Sleep = asyncio.sleep,
        stop_when: StopWhen | None = None,
    ) -> None:
        """Run with reconnect: back off on connection errors until unhealthy.

        Clean source exhaustion ends the loop. A connection error backs off
        (ROB-285 jittered exponential) and re-invokes the factory; once
        consecutive failures reach the unhealthy threshold the error is
        re-raised for the operator/supervisor above to handle.

        ``stop_when`` (bounded operator mode) is checked both at the top of each
        reconnect iteration and after each trigger inside ``run`` — so the count
        survives reconnects and the loop exits cleanly once the bound is hit.

        ``websockets.exceptions.ConnectionClosed`` (incl. ConnectionClosedOK/
        ConnectionClosedError) is caught explicitly — it is NOT a subclass of
        ConnectionError/OSError, so a real socket close would otherwise crash
        the daemon instead of reconnecting.
        """
        consecutive_failures = 0
        while True:
            if stop_when is not None and stop_when():
                return
            try:
                await self.run(
                    source_factory, on_trigger=on_trigger, stop_when=stop_when
                )
                return
            except (ConnectionError, OSError, ConnectionClosed) as exc:
                consecutive_failures += 1
                if is_unhealthy(consecutive_failures):
                    logger.error(
                        "WS daemon unhealthy after %d consecutive failures: %s",
                        consecutive_failures,
                        exc,
                    )
                    raise
                delay = compute_backoff_delay(consecutive_failures - 1)
                logger.warning(
                    "WS stream error (%s); reconnecting in %.2fs", exc, delay
                )
                await sleep(delay)
