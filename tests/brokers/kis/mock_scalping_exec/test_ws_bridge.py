"""Exec bridge tests (ROB-321 PR4b)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.signal import SignalDecision
from app.services.brokers.kis.mock_scalping_exec.ws_bridge import WsExecutionBridge
from app.services.brokers.kis.mock_scalping_ws.supervisor import TriggerEvent

SYMBOL = "005930"


def _trigger(side: str = "BUY") -> TriggerEvent:
    decision = SignalDecision(
        has_entry=True,
        side=side,
        entry_price=Decimal("70000"),
        tp_price=Decimal("70210"),
        sl_price=Decimal("69860"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
    )
    return TriggerEvent(
        symbol=SYMBOL,
        side=side,
        decision=decision,
        source_candle_close_time_ms=1,
        bid=70000.0,
        ask=70100.0,
        spread_bps=14.0,
        data_age_seconds=1.0,
        emitted_at=123.0,
    )


class FakeExecutor:
    def __init__(self, *, on_run=None):
        self.calls: list[tuple] = []
        self._on_run = on_run

    async def execute_monitored(self, intent, *, confirm):
        self.calls.append((intent, confirm))
        if self._on_run is not None:
            await self._on_run()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_runs_executor_with_intent_and_confirm() -> None:
    ex = FakeExecutor()
    bridge = WsExecutionBridge(executor=ex, confirm=True)
    await bridge.on_trigger(_trigger())
    assert len(ex.calls) == 1
    intent, confirm = ex.calls[0]
    assert intent.symbol == SYMBOL
    assert intent.side == "BUY"
    assert confirm is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_defaults_to_dry_run() -> None:
    ex = FakeExecutor()
    bridge = WsExecutionBridge(executor=ex)
    await bridge.on_trigger(_trigger())
    assert ex.calls[0][1] is False  # confirm defaults False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_skips_non_buy_trigger() -> None:
    ex = FakeExecutor()
    bridge = WsExecutionBridge(executor=ex, confirm=True)
    await bridge.on_trigger(_trigger(side="SELL"))  # build_order_intent -> None
    assert ex.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_per_symbol_in_flight_guard() -> None:
    # While one round trip for a symbol is running, a second trigger is skipped.
    gate = asyncio.Event()

    async def _block():
        await gate.wait()

    ex = FakeExecutor(on_run=_block)
    bridge = WsExecutionBridge(executor=ex, confirm=True)

    first = asyncio.create_task(bridge.on_trigger(_trigger()))
    await asyncio.sleep(0)  # let first enter execute_monitored
    await bridge.on_trigger(_trigger())  # second: skipped (in-flight)
    gate.set()
    await first

    assert len(ex.calls) == 1
