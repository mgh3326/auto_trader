"""ROB-321 PR4b — supervisor TriggerEvent → executor bridge.

The single mutation entry from the read-only supervisor (PR3) to the
confirm-gated executor (PR4a). Builds an ``OrderIntent`` from the trigger's
signal decision and runs the monitored round trip behind two concurrency guards:
a per-symbol in-flight set and a global semaphore (default cap 1 open lifecycle).

The bridge does NOT itself place orders or touch the ledger — it delegates to
the injected executor, which owns the confirm gate, fill confirmation, and the
ledger round-trip reconcile. ``confirm`` defaults to False (dry-run).
"""

from __future__ import annotations

import asyncio
import logging

from app.services.brokers.kis.mock_scalping.contract import ScalpingRiskLimits
from app.services.brokers.kis.mock_scalping.order_intent import build_order_intent
from app.services.brokers.kis.mock_scalping_ws.supervisor import TriggerEvent

logger = logging.getLogger("rob321.kis_mock_scalping_exec")


class WsExecutionBridge:
    def __init__(
        self,
        *,
        executor,
        limits: ScalpingRiskLimits | None = None,
        confirm: bool = False,
        max_concurrent: int = 1,
    ) -> None:
        self._executor = executor
        self._limits = limits or ScalpingRiskLimits()
        self._confirm = confirm
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight: set[str] = set()

    async def on_trigger(self, trigger: TriggerEvent) -> None:
        """Consume one TriggerEvent: build intent, run monitored round trip."""
        if trigger.symbol in self._in_flight:
            logger.info("bridge skip symbol=%s reason=in_flight", trigger.symbol)
            return

        intent = build_order_intent(
            trigger.decision,
            symbol=trigger.symbol,
            limits=self._limits,
            source_candle_close_time_ms=trigger.source_candle_close_time_ms,
            evaluated_at_ms=int(trigger.emitted_at * 1000),
        )
        if intent is None:
            logger.info("bridge skip symbol=%s reason=no_intent", trigger.symbol)
            return

        async with self._sem:
            self._in_flight.add(trigger.symbol)
            try:
                await self._executor.execute_monitored(intent, confirm=self._confirm)
            finally:
                self._in_flight.discard(trigger.symbol)
