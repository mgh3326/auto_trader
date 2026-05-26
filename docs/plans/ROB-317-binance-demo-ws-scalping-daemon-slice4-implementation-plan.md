# Binance Demo WS Scalping Daemon — Slice 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop — turn the slice-3 `TriggerEvent` stream into confirm-gated, concurrency-guarded **real Demo orders** via the existing `DemoScalpingExecutor`, wire it into the daemon CLI, and ship the operator runbook. This is the **only slice that can place orders** (demo-fapi, behind all three gates).

**Architecture:** The executor already owns the authoritative ledger-backed risk re-check (`_preflight`) and analytics — slice 4 does **not** re-implement them. The new `demo_scalping_exec/ws_bridge.py` is a thin `on_trigger` callback that: (1) applies an **in-process concurrency guard** the executor lacks (per-symbol in-flight + global cap), (2) builds an `OrderIntent` + `MarketConditions` from the trigger, (3) calls `execute_monitored(intent, confirm=mutation_allowed, market=...)`. The bridge lives on the **exec** side (it may import the executor/ledger); the read-only `demo_scalping_ws/` package still must not import the bridge — the AST guard stays green because the dependency direction is exec→ws (the bridge imports `TriggerEvent`), never ws→exec.

**Concurrency model (decision):** `on_trigger` is **awaited sequentially** by the supervisor. Because the global open-lifecycle cap is **1**, at most one position exists at a time, so sequential execution misses no tradeable opportunity (a second entry would be ledger-blocked anyway). The in-process guard is therefore enforced with **synchronous counters** (race-free in single-threaded asyncio) and is proven by invoking `on_trigger` concurrently in tests. Known tradeoff: while a bounded monitor runs (≤ `max_runtime_s`), the supervisor pauses consuming events; on resume the freshness gate blocks until quotes are fresh again. If true multi-symbol concurrent trading is ever wanted (global cap > 1), the bridge would move to background-task dispatch — out of scope here.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `pytest-asyncio`, `ruff`, stdlib `asyncio`/`datetime`/`decimal`. Reuses `DemoScalpingExecutor`, `build_order_intent`, `MarketConditions`, `BinanceFuturesDemoExecutionClient`, `DemoScalpingMarketData`, `DemoReferenceData`, `AsyncSessionLocal`.

---

## Boundary & reuse notes (read before starting)

- `ws_bridge.py` goes in `app/services/brokers/binance/demo_scalping_exec/` (the **mutation** package), NOT `demo_scalping_ws/`. It imports `TriggerEvent` from `demo_scalping_ws.supervisor` (exec→ws, allowed) and the executor/intent/clients (same package / contract).
- The executor's `execute_monitored(intent, *, confirm=False, market=None, ...)` performs the live-ledger risk re-check in `_preflight` and writes analytics in `_finalize_analytics`. **Do not duplicate** risk or analytics in the bridge.
- `confirm=False` ⇒ the executor/client place **no** orders (dry-run); this is already enforced and tested at the client/executor layer. The bridge's job is to pass `confirm=gates.mutation_allowed` faithfully.
- `build_order_intent(decision, *, product, symbol, limits, source_candle_close_time_ms, evaluated_at_ms)` needs the source candle's close time — slice 3's `TriggerEvent` does not carry it, so **Task 1 adds that field**.
- The daemon trades **futures only** (`product="usdm_futures"`); the symbol allowlist + caps come from `ScalpingRiskLimits` (unchanged).

## File Structure

| File | Responsibility |
|---|---|
| `app/services/brokers/binance/demo_scalping_ws/supervisor.py` (modify) | Add `source_candle_close_time_ms` to `TriggerEvent`; populate it from the closing kline |
| `app/services/brokers/binance/demo_scalping_exec/ws_bridge.py` (create) | `WsExecutionBridge` (guard + intent + confirm passthrough), `make_demo_futures_trade_runner`, `build_ws_execution_bridge_from_env` |
| `scripts/binance_demo_scalping_ws_daemon.py` (modify) | Active path uses the bridge as `on_trigger` (confirm=`mutation_allowed`); injectable for tests |
| `docs/runbooks/binance-demo-ws-scalping.md` (create) | Operator runbook (startup/dry-run/confirm/health/stop/rollback/failures) |
| `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py` (modify) | Assert `source_candle_close_time_ms` on the emitted trigger |
| `tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py` (create) | confirm passthrough, concurrency guard, release on success/exception, intent-None |
| `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py` (modify) | Active path wires the bridge; injected `on_trigger` |

---

### Task 1: Carry the source candle close time on `TriggerEvent`

`build_order_intent` requires `source_candle_close_time_ms`. Propagate it from the closing kline through the trigger.

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_ws/supervisor.py`
- Test: `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py`:

```python
async def test_trigger_carries_source_candle_close_time() -> None:
    clock = _Clock(_T0 + dt.timedelta(seconds=5))
    sup = ScalpingDaemonSupervisor(symbols=["XRPUSDT"], clock=clock)
    captured: list[TriggerEvent] = []
    await sup.run(
        lambda: _source_from(_breakout_sequence()),
        on_trigger=_async_appender(captured),
    )
    assert len(captured) == 1
    # The breakout candle is minute=25; its close_time is open+59s.
    expected_close = _T0 + dt.timedelta(minutes=25, seconds=59)
    assert captured[0].source_candle_close_time_ms == int(
        expected_close.timestamp() * 1000
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -k source_candle -v`
Expected: FAIL — `AttributeError: 'TriggerEvent' object has no attribute 'source_candle_close_time_ms'`.

- [ ] **Step 3: Add the field and populate it**

In `supervisor.py`, add the field to `TriggerEvent` (after `decision`):

```python
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
```

Thread the closing kline's close time from `_handle_event` into `_gate_and_build`. In `_handle_event`, change the kline branch:

```python
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
```

Update `_gate_and_build`'s signature and the `TriggerEvent(...)` construction:

```python
    def _gate_and_build(
        self,
        symbol: str,
        state: MarketState,
        decision: SignalDecision,
        now: dt.datetime,
        *,
        source_candle_close_time_ms: int,
    ) -> TriggerEvent | None:
        # ... freshness + debounce gates unchanged ...
        return TriggerEvent(
            product=self._product,
            symbol=symbol,
            side=decision.side,
            decision=decision,
            source_candle_close_time_ms=source_candle_close_time_ms,
            bid_price=state.bid_price,
            ask_price=state.ask_price,
            data_age_seconds=age,
            emitted_at=now,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py -v`
Expected: PASS (all prior supervisor tests + the new one). The existing tests assert `.symbol`/`.side` and trigger counts, which are unaffected by the added field.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_ws/supervisor.py \
        tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py
git commit -m "$(cat <<'EOF'
feat(rob-317): carry source candle close time on TriggerEvent

Slice 4. build_order_intent needs source_candle_close_time_ms; propagate it
from the closing kline through the trigger.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `WsExecutionBridge` — guard + intent + confirm passthrough

The bridge is the `on_trigger` callback. It enforces the in-process concurrency guard (per-symbol in-flight + global cap) that the executor lacks, builds the intent + market snapshot, and delegates the trade to an injectable `trade_runner` (the real one is Task 3; tests pass a fake). Risk re-check and analytics live inside the runner's executor — not here.

**Files:**
- Create: `app/services/brokers/binance/demo_scalping_exec/ws_bridge.py`
- Test: `tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/brokers/binance/demo_scalping_exec/__init__.py` (empty if absent) and `tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError: ... ws_bridge`.

- [ ] **Step 3: Create `ws_bridge.py` (bridge + guard only)**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_exec/ws_bridge.py \
        tests/services/brokers/binance/demo_scalping_exec/__init__.py \
        tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py
git commit -m "$(cat <<'EOF'
feat(rob-317): WsExecutionBridge — concurrency guard + confirm passthrough

Slice 4. on_trigger callback adds the in-process per-symbol + global-cap guard
(synchronous counters, race-free) the executor lacks, builds intent + market,
and passes confirm through to an injectable trade_runner. Risk re-check +
analytics stay in the executor. Guard released on success and exception.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Real trade runner + env factory

The real `trade_runner` constructs a per-trade DB session + executor and calls `execute_monitored`, mirroring `app/jobs/binance_demo_scalping_runner.py`. The env factory builds the futures client + market data + reference and wires a ready bridge.

**Files:**
- Modify: `app/services/brokers/binance/demo_scalping_exec/ws_bridge.py`
- Test: append to `tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py`:

```python
async def test_make_demo_futures_trade_runner_calls_execute_monitored() -> None:
    seen: dict[str, object] = {}

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def commit(self) -> None:
            seen["committed"] = True

    class _FakeExecutor:
        def __init__(self, **kwargs) -> None:
            seen["product"] = kwargs["product"]
            seen["now"] = kwargs["now"]

        async def execute_monitored(self, intent, *, confirm, market) -> object:
            seen["confirm"] = confirm
            seen["symbol"] = intent.symbol
            return SimpleNamespace(status="filled")

    from app.services.brokers.binance.demo_scalping_exec import ws_bridge as mod

    runner = mod.make_demo_futures_trade_runner(
        client=object(),
        market_data=object(),
        reference=object(),
        session_factory=lambda: _FakeSession(),
        limits=ScalpingRiskLimits(),
        executor_cls=_FakeExecutor,
    )
    intent = build_order_intent(
        _trigger().decision, product="usdm_futures", symbol="XRPUSDT",
        limits=ScalpingRiskLimits(), source_candle_close_time_ms=1, evaluated_at_ms=2,
    )
    result = await runner(intent, _market(), True, _T0)
    assert seen == {
        "product": "usdm_futures", "now": _T0, "confirm": True,
        "symbol": "XRPUSDT", "committed": True,
    }
    assert result.status == "filled"


def _market() -> object:
    from app.services.brokers.binance.demo_scalping.contract import MarketConditions
    return MarketConditions(
        spread_bps=Decimal("1"), data_age_seconds=2.0, spot_free_base_qty=Decimal("0")
    )
```

Add this import near the top of the test file:

```python
from app.services.brokers.binance.demo_scalping.order_intent import build_order_intent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py -k trade_runner -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'make_demo_futures_trade_runner'`.

- [ ] **Step 3: Add the runner + env factory to `ws_bridge.py`**

Add a session-factory type alias near the other aliases (`Callable`/`Any` are already imported from Task 2 — add no new imports):

```python
SessionFactory = Callable[[], Any]
```

Append to `ws_bridge.py`:

```python
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
) -> tuple["WsExecutionBridge", Callable[[], Awaitable[None]]]:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/binance/demo_scalping_exec/ws_bridge.py \
        tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py
git commit -m "$(cat <<'EOF'
feat(rob-317): real futures trade runner + env-built bridge factory

Slice 4. make_demo_futures_trade_runner opens a per-trade session + executor
and calls execute_monitored (mirrors the scheduler runner). build_ws_execution
_bridge_from_env wires futures client + market data + reference + cleanup; lazy
imports keep the disabled path broker/DB-free.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the CLI active path to the bridge

Replace the slice-3 log-only sink: when `daemon_active`, build the bridge from env with `confirm=gates.mutation_allowed` and use it as `on_trigger`. Keep `on_trigger` injectable so tests stay network/DB-free.

**Files:**
- Modify: `scripts/binance_demo_scalping_ws_daemon.py`
- Test: `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`

- [ ] **Step 1: Write the failing test**

Replace the slice-3 `test_run_daemon_logs_triggers_without_mutation` test with an injected-`on_trigger` version, and add a wiring assertion. In `tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py`:

```python
@pytest.mark.asyncio
async def test_run_daemon_routes_triggers_to_injected_on_trigger() -> None:
    seen: list[str] = []

    async def fake_on_trigger(trigger) -> None:
        seen.append(trigger.symbol)

    await run_daemon(
        symbols=["XRPUSDT"],
        source_factory=lambda: _source(_seq()),
        on_trigger=fake_on_trigger,
        clock=lambda: _T0 + dt.timedelta(seconds=5),
    )
    assert seen == ["XRPUSDT"]  # one BUY breakout routed to the sink
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -k routes_triggers -v`
Expected: FAIL — `TypeError: run_daemon() got an unexpected keyword argument 'on_trigger'` (slice 3 had no `on_trigger` param).

- [ ] **Step 3: Update `run_daemon` to accept an injectable `on_trigger` and default to the bridge**

Replace the slice-3 `run_daemon` body (and its imports) in `scripts/binance_demo_scalping_ws_daemon.py`:

```python
from collections.abc import AsyncIterator, Awaitable, Callable

from app.services.brokers.binance.demo_scalping_ws.supervisor import (
    ScalpingDaemonSupervisor,
    TriggerEvent,
)

OnTrigger = Callable[[TriggerEvent], Awaitable[None]]


async def run_daemon(
    *,
    symbols: list[str],
    source_factory: Callable[[], AsyncIterator[FuturesWsEvent]] | None = None,
    on_trigger: OnTrigger | None = None,
    confirm: bool = False,
    clock: Callable[[], dt.datetime] | None = None,
) -> None:
    """Run the trigger pipeline.

    Production: ``on_trigger`` defaults to the env-built WsExecutionBridge
    (confirm passed in from gates). Tests inject ``source_factory`` +
    ``on_trigger`` to stay network/DB-free.
    """
    factory = source_factory or _real_source_factory(symbols)
    sup = ScalpingDaemonSupervisor(
        symbols=symbols, **({"clock": clock} if clock else {})
    )
    aclose: Callable[[], Awaitable[None]] | None = None
    if on_trigger is None:
        from app.services.brokers.binance.demo_scalping_exec.ws_bridge import (
            build_ws_execution_bridge_from_env,
        )

        bridge, aclose = await build_ws_execution_bridge_from_env(confirm=confirm)
        on_trigger = bridge
    try:
        await sup.run_with_reconnect(factory, on_trigger=on_trigger)
    finally:
        if aclose is not None:
            await aclose()
```

Update `main` so the active path passes `confirm=gates.mutation_allowed` (the disabled path is unchanged):

```python
    asyncio.run(run_daemon(symbols=symbols, confirm=gates.mutation_allowed))
```

Remove the now-unused slice-3 trigger-counting `on_trigger` closure and the `count`/`return count` from `run_daemon` (it no longer returns a count). If any slice-3 test asserted a return count, it was replaced in Step 1.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py -v`
Expected: PASS (disabled-path tests + `running` status test + the new injected-`on_trigger` test).

- [ ] **Step 5: Commit**

```bash
git add scripts/binance_demo_scalping_ws_daemon.py \
        tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
git commit -m "$(cat <<'EOF'
feat(rob-317): CLI active path executes via WsExecutionBridge

Slice 4. daemon_active builds the env bridge with confirm=mutation_allowed and
routes triggers to it; confirm=false runs the executor in dry-run (no order).
on_trigger stays injectable for network/DB-free tests.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Operator runbook

**Files:**
- Create: `docs/runbooks/binance-demo-ws-scalping.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/binance-demo-ws-scalping.md` with these sections (prose, mirroring `docs/runbooks/binance-futures-demo-smoke.md` house style):

1. **Scope & lane boundaries** — read-only market data on `fstream.binance.com`; order mutation demo-only on `demo-fapi.binance.com`; live/testnet refused fail-closed. Reference the design doc.
2. **Preconditions** — `BINANCE_FUTURES_DEMO_*` / canonical `BINANCE_DEMO_*` creds present; DB reachable; ledger migrated.
3. **Three gates** — `BINANCE_DEMO_SCALPING_ENABLED`, `BINANCE_DEMO_SCALPING_WS_ENABLED`, `BINANCE_DEMO_SCALPING_WS_CONFIRM`; the gate-behavior table (disabled / dry-run / confirmed).
4. **Default-disabled startup** — `uv run python -m scripts.binance_demo_scalping_ws_daemon` → `{"status":"disabled",...}`, exit 0, no subscribe.
5. **Dry-run** — `ENABLED=true WS_ENABLED=true WS_CONFIRM=false` → subscribes fstream, evaluates triggers, runs the executor in dry-run (zero broker mutation). Watch structured `ws_bridge`/`trigger` logs.
6. **Confirmed Demo startup** — all three true → real Demo orders on demo-fapi only; every order still subject to risk gates + the in-process concurrency guard (one open lifecycle). Verify via the ledger after the first round-trip.
7. **Health check** — poll the `health.py` snapshot; confirm connected + per-symbol freshness < 120s + last outcome sane.
8. **Stop / rollback** — stop the process; set `WS_ENABLED=false` to disable without removing config; reconcile any open position via the futures-demo smoke CLI; the 5-min Prefect tick remains the fallback observation path.
9. **Failure categories** — stream disconnect (reconnect/backoff), stale data (`STALE_DATA` blocks), risk block (reason codes logged, no order), concurrency skip (`OPEN_LIFECYCLE_EXISTS`/`GLOBAL_LIFECYCLE_CAP_REACHED`), credential/DB unavailable (fail closed), confirm off (dry-run).
10. **Relationship to the 5-min Prefect tick** — reclassified as polling intraday / smoke; pausing it is a deferred operator decision.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/binance-demo-ws-scalping.md
git commit -m "$(cat <<'EOF'
docs(rob-317): operator runbook for the WS scalping daemon

Slice 4. Startup (disabled/dry-run/confirmed), health, stop/rollback, failure
categories, lane boundaries, and the relationship to the 5-min Prefect tick.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Slice-wide verification + handoff

**Files:** none (verification only).

- [ ] **Step 1: Run all ROB-317 daemon tests + the import guard**

Run:
```bash
uv run pytest \
  tests/services/brokers/binance/demo_scalping_ws/ \
  tests/services/brokers/binance/demo_scalping_exec/test_ws_bridge.py \
  tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py \
  tests/services/brokers/binance/demo/test_no_testnet_imports.py -v
```
Expected: PASS (all). The import guard must stay green: `ws_bridge.py` is in `demo_scalping_exec/` (allowed to import the executor), and `demo_scalping_ws/` still imports nothing from the exec side — the dependency is exec→ws (`ws_bridge` imports `TriggerEvent`), never ws→exec.

- [ ] **Step 2: Lint changed surfaces**

Run:
```bash
uv run ruff check \
  app/services/brokers/binance/demo_scalping_ws/supervisor.py \
  app/services/brokers/binance/demo_scalping_exec/ws_bridge.py \
  scripts/binance_demo_scalping_ws_daemon.py \
  tests/services/brokers/binance/demo_scalping_exec/ \
  tests/services/brokers/binance/demo_scalping_ws/test_supervisor.py \
  tests/scripts/test_binance_demo_scalping_ws_daemon_cli.py
```
Expected: no errors. Run `uv run ruff format` on the same paths if needed, then amend.

- [ ] **Step 3: Confirm the disabled path is still fully inert (no broker/DB import reached)**

Run: `env -u BINANCE_DEMO_SCALPING_ENABLED -u BINANCE_DEMO_SCALPING_WS_ENABLED -u BINANCE_DEMO_SCALPING_WS_CONFIRM uv run python -m scripts.binance_demo_scalping_ws_daemon`
Expected: `{"base_enabled": false, "status": "disabled", "subscribed": false, "ws_enabled": false}`, exit 0. (The bridge/executor are lazily imported only on the active path, so the disabled run touches no broker/DB module.)

- [ ] **Step 4: Full broker regression sweep**

Run: `uv run pytest tests/services/brokers/binance/ -q`
Expected: PASS — slice-4 additions plus all existing ROB-285/298/307/313/315 broker tests green.

- [ ] **Step 5: Run the full repo import guards + targeted lint (pre-merge gate)**

Run:
```bash
uv run ruff check app/ tests/ scripts/
uv run pytest tests/services/brokers/binance/demo/test_no_testnet_imports.py -q
```
Expected: clean. (Per the project's pre-merge full-CI gate: branch protection does not gate lint/test, so confirm these before any merge.)

---

## Self-Review

**Spec coverage (design §13 slice-4 scope + §10 remaining test-list items):**
- `ws_bridge.py`: trigger → confirm-gated executor (design §13) → Task 2, Task 3 ✓
- Live ledger risk re-check before executor call (design §6.2) → delegated to the executor's `_preflight` (reused, not duplicated); the bridge adds the in-process guard → Task 2 ✓
- Two-layer concurrency guard / one-open-lifecycle / in-flight duplicate (design §6.2; test-list) → Task 2 (in-process counters + concurrent-call tests) + executor `_preflight` (durable DB backstop) ✓
- `confirm=false` blocks mutation (test-list) → Task 2 (passthrough) + Task 4 (CLI confirm=mutation_allowed); enforced at the client/executor layer ✓
- Risk-gate-blocked path logs reason codes, no order (test-list) → executor `_preflight` (existing coverage); bridge logs concurrency-skip reason codes ✓
- Analytics/review wiring (design §13) → executor `_finalize_analytics` (reused) ✓
- Dedicated runbook (design §13, §14) → Task 5 ✓
- Handoff checklist (design §14) → Task 6 + the handoff note below ✓

**Placeholder scan:** No "TBD"/"implement later". Every code step ships complete code. Task 5 (runbook) is a prose-section list because the runbook is documentation, not code — each section is specified concretely.

**Type consistency:**
- `TriggerEvent` gains `source_candle_close_time_ms: int` (Task 1) and the bridge reads it (Task 2) — consistent.
- `TradeRunner = (OrderIntent, MarketConditions, bool, dt.datetime) -> Awaitable[Any]` used identically by `WsExecutionBridge.__call__`, `make_demo_futures_trade_runner`, and the bridge tests (Tasks 2, 3).
- `WsExecutionBridge(trade_runner=, limits=, confirm=, clock=)` consistent across Tasks 2, 3, 4.
- `build_ws_execution_bridge_from_env(*, confirm, clock=, limits=) -> (bridge, aclose)` consistent (Task 3) with its CLI call site (Task 4).
- `run_daemon(*, symbols, source_factory=, on_trigger=, confirm=, clock=)` consistent between Task 4's implementation and tests; the slice-3 trigger-count return is removed and its test replaced in the same task — no stale assertion.

**Cross-slice note:** Task 1 modifies the slice-3 `supervisor.py`/`test_supervisor.py` (adds a field) and Task 4 modifies the slice-3 CLI + test (swaps the log-only sink). Both are additive/explicit and update their tests in the same task.

---

## Handoff (per design §14, fill in at PR time)

- Branch `rob-317`; PR URL: _(to add)_.
- Files changed: `supervisor.py`, `ws_bridge.py` (new), `binance_demo_scalping_ws_daemon.py`, `binance-demo-ws-scalping.md` (new) + tests.
- Tests run + results: the Task 6 commands and their pass counts.
- Migrations: **none** — reuses `binance_demo_order_ledger` + `scalp_trade_analytics` (no schema change).
- Env flags: dry-run = `BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_WS_ENABLED=true` (confirm unset); confirmed = add `BINANCE_DEMO_SCALPING_WS_CONFIRM=true`.
- Explicit statement: no live orders, no production scheduler/launchd mutation, no secret logging performed.
- Recommended Hermes post-merge checks: poll the health snapshot for liveness; after the first confirmed round-trip, spot-check the `binance_demo_order_ledger` + `scalp_trade_analytics` rows.
- This completes the ROB-317 daemon (slices 1–4). The 5-min Prefect tick remains; pausing it is a deferred operator decision.
