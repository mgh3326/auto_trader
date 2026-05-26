"""ROB-317 — WS trigger → Demo executor bridge (the only mutation path).

The supervisor (read-only package) emits TriggerEvents; this exec-side
callback turns an allowed, confirmed trigger into a real Demo order via the
existing DemoScalpingExecutor. The executor owns the authoritative
ledger-backed risk re-check and analytics — this bridge adds only the
in-process concurrency guard the executor lacks, builds the order intent +
market snapshot, and passes the confirm flag through.

Concurrency: on_trigger is awaited sequentially by the supervisor; with a
global open-lifecycle cap of 1 at most one position exists at a time. The
guard uses synchronous counters (race-free in single-threaded asyncio) so a
second trigger for the same symbol — or any symbol once the global cap is
reached — is skipped rather than double-entered. See ROB-317 design §6.2.

No live host, no order placed unless ``confirm=True`` (which the client/
executor layer enforces and tests).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import (
    OrderIntent,
    build_order_intent,
)
from app.services.brokers.binance.demo_scalping_ws.supervisor import TriggerEvent

logger = logging.getLogger("rob317.ws_bridge")

# (intent, market, confirm, now) -> execution result
TradeRunner = Callable[
    [OrderIntent, MarketConditions, bool, dt.datetime], Awaitable[Any]
]
Clock = Callable[[], dt.datetime]
SessionFactory = Callable[[], Any]


def _default_clock() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _spread_bps(bid: Decimal | None, ask: Decimal | None) -> Decimal:
    """Best-bid/ask spread in bps; 0 when a side is missing (preflight will
    re-evaluate against the executor's own snapshot too)."""
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return Decimal("0")
    mid = (bid + ask) / Decimal("2")
    return (ask - bid) / mid * Decimal("10000")


class WsExecutionBridge:
    """Trigger → confirm-gated, concurrency-guarded Demo executor call."""

    def __init__(
        self,
        *,
        trade_runner: TradeRunner,
        limits: ScalpingRiskLimits | None = None,
        confirm: bool = False,
        clock: Clock = _default_clock,
    ) -> None:
        self._trade_runner = trade_runner
        self._limits = limits or ScalpingRiskLimits()
        self._confirm = confirm
        self._clock = clock
        self._global_cap = self._limits.global_open_lifecycle_cap
        self._inflight: set[str] = set()
        self._global_inflight = 0

    async def __call__(self, trigger: TriggerEvent) -> None:
        symbol = trigger.symbol
        # Synchronous guard checks + reservation: no await between check and
        # reserve, so this is race-free under single-threaded asyncio.
        if symbol in self._inflight:
            logger.info(
                "ws_bridge skip symbol=%s reason=%s",
                symbol,
                ReasonCode.OPEN_LIFECYCLE_EXISTS,
            )
            return
        if self._global_inflight >= self._global_cap:
            logger.info(
                "ws_bridge skip symbol=%s reason=%s",
                symbol,
                ReasonCode.GLOBAL_LIFECYCLE_CAP_REACHED,
            )
            return
        self._inflight.add(symbol)
        self._global_inflight += 1
        try:
            now = self._clock()
            intent = build_order_intent(
                trigger.decision,
                product=trigger.product,
                symbol=symbol,
                limits=self._limits,
                source_candle_close_time_ms=trigger.source_candle_close_time_ms,
                evaluated_at_ms=int(now.timestamp() * 1000),
            )
            if intent is None:
                return
            market = MarketConditions(
                spread_bps=_spread_bps(trigger.bid_price, trigger.ask_price),
                data_age_seconds=trigger.data_age_seconds or 0.0,
                spot_free_base_qty=Decimal("0"),
            )
            result = await self._trade_runner(intent, market, self._confirm, now)
            logger.info(
                "ws_bridge executed symbol=%s side=%s confirm=%s status=%s",
                symbol,
                trigger.side,
                self._confirm,
                getattr(result, "status", None),
            )
        finally:
            self._inflight.discard(symbol)
            self._global_inflight -= 1


def make_demo_futures_trade_runner(
    *,
    client: Any,
    market_data: Any,
    reference: Any,
    session_factory: SessionFactory,
    limits: ScalpingRiskLimits,
    executor_cls: Any | None = None,
) -> TradeRunner:
    """Build a TradeRunner that opens a per-trade session + executor.

    ``executor_cls`` is injectable for tests; production uses the real
    DemoScalpingExecutor (imported lazily so the disabled path never touches
    broker/DB modules). Mirrors app/jobs/binance_demo_scalping_runner.py.
    """
    if executor_cls is None:
        from app.services.brokers.binance.demo_scalping_exec.executor import (
            DemoScalpingExecutor,
        )

        executor_cls = DemoScalpingExecutor

    async def _run(
        intent: OrderIntent,
        market: MarketConditions,
        confirm: bool,
        now: dt.datetime,
    ) -> Any:
        async with session_factory() as session:
            executor = executor_cls(
                product="usdm_futures",
                client=client,
                session=session,
                reference=reference,
                now=now,
                market_data=market_data,
                limits=limits,
            )
            result = await executor.execute_monitored(
                intent, confirm=confirm, market=market
            )
            await session.commit()
            return result

    return _run


async def build_ws_execution_bridge_from_env(
    *,
    confirm: bool,
    clock: Clock = _default_clock,
    limits: ScalpingRiskLimits | None = None,
) -> tuple[WsExecutionBridge, Callable[[], Awaitable[None]]]:
    """Construct a futures Demo bridge from env + its async cleanup.

    Lazy imports keep the disabled CLI path free of broker/DB setup. Futures
    only; demo-fapi only (host-guarded at the client transport).
    """
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
    )
    from app.services.brokers.binance.demo_scalping_exec.reference import (
        DemoReferenceData,
    )
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )

    resolved_limits = limits or ScalpingRiskLimits()
    client = BinanceFuturesDemoExecutionClient.from_env()
    market_data = DemoScalpingMarketData()
    reference = DemoReferenceData()
    runner = make_demo_futures_trade_runner(
        client=client,
        market_data=market_data,
        reference=reference,
        session_factory=AsyncSessionLocal,
        limits=resolved_limits,
    )
    bridge = WsExecutionBridge(
        trade_runner=runner, limits=resolved_limits, confirm=confirm, clock=clock
    )

    async def _aclose() -> None:
        await market_data.aclose()
        await reference.aclose()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()

    return bridge, _aclose
