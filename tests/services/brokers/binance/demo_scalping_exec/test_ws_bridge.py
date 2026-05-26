"""ROB-317 — WsExecutionBridge guard + confirm passthrough."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.signal import SignalDecision
from app.services.brokers.binance.demo_scalping_ws.supervisor import TriggerEvent
from app.services.brokers.binance.demo_scalping_exec.ws_bridge import WsExecutionBridge

pytestmark = pytest.mark.asyncio

_T0 = dt.datetime(2026, 5, 26, 10, 0, tzinfo=dt.UTC)


def _trigger(symbol: str = "XRPUSDT", side: str = "BUY") -> TriggerEvent:
    decision = SignalDecision(
        has_entry=True, side=side, entry_price=Decimal("0.60"),
        tp_price=Decimal("0.62"), sl_price=Decimal("0.59"),
        confidence=Decimal("0.8"), reason_codes=("enter_long_breakout",),
    )
    return TriggerEvent(
        product="usdm_futures", symbol=symbol, side=side, decision=decision,
        source_candle_close_time_ms=1716724799999,
        bid_price=Decimal("0.5999"), ask_price=Decimal("0.6001"),
        data_age_seconds=3.0, emitted_at=_T0,
    )


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, intent, market, confirm, now) -> object:
        self.calls.append((intent.symbol, confirm))
        return SimpleNamespace(status="filled")


def _bridge(runner, *, confirm: bool, global_cap: int = 1) -> WsExecutionBridge:
    return WsExecutionBridge(
        trade_runner=runner,
        limits=ScalpingRiskLimits(global_open_lifecycle_cap=global_cap),
        confirm=confirm,
        clock=lambda: _T0,
    )


async def test_confirm_true_passes_through() -> None:
    runner = _RecordingRunner()
    await _bridge(runner, confirm=True)(_trigger())
    assert runner.calls == [("XRPUSDT", True)]


async def test_confirm_false_passes_through_no_mutation_flag() -> None:
    runner = _RecordingRunner()
    await _bridge(runner, confirm=False)(_trigger())
    assert runner.calls == [("XRPUSDT", False)]


async def test_same_symbol_inflight_is_skipped() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _Blocking:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __call__(self, intent, market, confirm, now) -> object:
            self.calls.append(intent.symbol)
            started.set()
            await release.wait()
            return SimpleNamespace(status="filled")

    runner = _Blocking()
    bridge = _bridge(runner, confirm=True)
    t1 = asyncio.create_task(bridge(_trigger("XRPUSDT")))
    await started.wait()  # t1 has entered the runner and holds the guard
    await bridge(_trigger("XRPUSDT"))  # second same-symbol call: skipped immediately
    release.set()
    await t1
    assert runner.calls == ["XRPUSDT"]  # only one entry ran


async def test_global_cap_blocks_other_symbol() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _Blocking:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __call__(self, intent, market, confirm, now) -> object:
            self.calls.append(intent.symbol)
            started.set()
            await release.wait()
            return SimpleNamespace(status="filled")

    runner = _Blocking()
    bridge = _bridge(runner, confirm=True, global_cap=1)
    t1 = asyncio.create_task(bridge(_trigger("XRPUSDT")))
    await started.wait()
    await bridge(_trigger("DOGEUSDT"))  # different symbol, but global cap=1 -> skip
    release.set()
    await t1
    assert runner.calls == ["XRPUSDT"]


async def test_guard_released_after_completion() -> None:
    runner = _RecordingRunner()
    bridge = _bridge(runner, confirm=True)
    await bridge(_trigger("XRPUSDT"))
    await bridge(_trigger("XRPUSDT"))  # guard freed -> second runs
    assert runner.calls == [("XRPUSDT", True), ("XRPUSDT", True)]


async def test_guard_released_on_runner_exception() -> None:
    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, intent, market, confirm, now) -> object:
            self.calls += 1
            raise RuntimeError("executor blew up")

    runner = _Boom()
    bridge = _bridge(runner, confirm=True)
    with pytest.raises(RuntimeError):
        await bridge(_trigger("XRPUSDT"))
    with pytest.raises(RuntimeError):
        await bridge(_trigger("XRPUSDT"))  # guard not leaked
    assert runner.calls == 2
