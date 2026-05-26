# ROB-321 KIS mock Scalping Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a KIS official mock (`account_mode=kis_mock`) scalping WebSocket execution loop — entry signal → mock buy → fill → TP/SL/time-stop → mock exit → round-trip ledger close — as **edge-agnostic execution plumbing** (the strategy edge is known net-negative per ROB-316; v1 strategy is intentionally a conservative toy whose only job is to exercise the plumbing).

**Architecture:** Mirror the Binance Demo scalping pattern (ROB-317): a read-only **live** KIS quote/orderbook WebSocket feeds a deterministic signal/risk contract; an import-guarded execution bridge is the only path that mutates, routing orders to **mock** only. Market-data (live) and order (mock) transports are host-separated and fail-closed. The `avg*1.01` sell floor and the `price < current_price` guard are bypassed **only** for mock scalping exits via an explicit, flag-gated context object that mirrors the existing `DefensiveTrimContext` mechanism. Live/generic order paths are never touched.

**Tech Stack:** Python 3.13, FastAPI, asyncio, pytest (`-m unit`/`-m integration`), SQLAlchemy async + Alembic, KIS WebSocket (approval-key handshake), existing `KISMockOrderLedger` shadow ledger.

**PR slicing (each PR lands independently, full-CI green before merge):**
1. **PR1 — Mock scalping sell-guard separation (pure validation logic).** ← *fully detailed below.*
2. **PR2 — Live KIS quote/orderbook WebSocket (read-only) + host separation.** ← *architected below; detailed plan authored when reached.*
3. **PR3 — Entry/strategy/risk contract + supervisor (default-off, dry-run).** ← *architected below.*
4. **PR4 — Execution bridge + round-trip ledger/reconcile + smoke runbook.** ← *architected below.*

> **Scope note (per writing-plans scope check):** This feature spans four independent subsystems. PR1 is specified to bite-sized step level here because it is fully determinable from the current codebase. PR2–PR4 each depend on interfaces that PR1 (and earlier PRs) land, and on internals (KIS WS protocol, executor) that must be read against the merged code before writing faithful step-level code. Their **file structure, public interfaces, and test strategy are locked below**; author the step-level plan for each as a sibling document (`ROB-321-prN-*.md`) at execution time. Do not write fictional step code for PR2–4 now.

---

## Cross-cutting constraints (apply to every PR)

- **Live untouched:** No change to KIS live / generic live order validation. The `avg*1.01` floor and `price < current_price` guard remain fully enforced for `is_mock=False`. Proven by a test that runs the live path and asserts both guards still fire.
- **Fail-closed activation:** The scalping exit bypass is constructible **only** when `is_mock=True` **and** `settings.kis_mock_scalping_enabled` is true. `is_mock=False` → raise. Flag off → raise. Default flag = `False`.
- **Host separation (PR2+):** Market data = live KIS quote WS (read-only). Orders = mock only. Enforced at the transport layer via host allowlist, not config flags alone (per `feedback_transport_layer_fail_closed`). Mock order host stays `openapivts.koreainvestment.com`; live quote host is distinct and may never carry an order mutation.
- **No scheduler:** No TaskIQ/Prefect/cron registration or always-on activation in any PR. CLI/daemon entrypoint only, default-disabled.
- **No prod mutation:** No production DB backfill, no live journal changes.
- **Account-mode logging:** Every scalping execution log line carries `account_mode="kis_mock"`.
- **Out of PR1 scope (live safety boundary):** `app/services/kis_trading_service.py:402` and `app/services/order_service.py:65` carry their own `>= avg*1.01 and >= current_price` filters for KIS auto-trade / crypto order services. The scalping loop does **not** route through these; they are intentionally **not modified** — leaving them untouched preserves live safety.

---

## File Structure (whole feature)

**PR1 (this document, detailed):**
- Modify: `app/core/config.py` — add `kis_mock_scalping_enabled: bool = False`.
- Modify: `app/mcp_server/tooling/order_validation.py` — add `ScalpingExitContext`, `evaluate_sell_price_guards()` (pure), `_resolve_scalping_exit_context()`, `_log_scalping_exit_bypass()`; route `_preview_sell` and `_validate_sell_side` through the pure function; thread `scalping_exit_ctx`.
- Create: `tests/test_kis_mock_scalping_sell_guard.py` — pure-function matrix, resolver fail-closed, preview/validate wiring, live-unaffected regression.

**PR2 (architected):**
- Modify: `app/services/kis_websocket_internal/protocol.py` — add quote/orderbook TR codes (`H0STCNT0` 체결가, `H0STASP0` 호가; overseas equivalents) as a **separate** subscription set from execution TRs.
- Create: `app/services/kis_websocket_internal/quote_parsers.py` — parse quote/orderbook frames → `QuoteTick`/`OrderBookSnapshot` (do not touch `parsers.py` which is execution-only).
- Create: `app/services/brokers/kis/mock_scalping_ws/market_stream.py` — read-only live quote WS client (reconnect/backoff/heartbeat copied from `KISExecutionWebSocket` pattern); host allowlist = live quote host only.
- Create: `app/services/brokers/kis/mock_scalping_ws/state.py` — per-symbol `MarketState` (bid/ask/qty, last trade, candle aggregation, per-stream freshness timestamps).
- Create: `scripts/kis_mock_scalping_ws_smoke.py` — read-only tick smoke (no orders).

**PR3 (architected):**
- Create: `app/services/brokers/kis/mock_scalping/contract.py` — `ScalpingRiskLimits`, `ReasonCode`, `evaluate_risk()` (KIS allowlist, max notional, max open positions, per-symbol cooldown, daily attempt/loss cap, spread guard, data-freshness guard). Mirror `binance/demo_scalping/contract.py`.
- Create: `app/services/brokers/kis/mock_scalping/signal.py` — pure `evaluate_signal(candles, config) -> SignalDecision` (toy conservative v1: liquidity filter + pullback/no-chase + spread check; single entry per symbol).
- Create: `app/services/brokers/kis/mock_scalping/order_intent.py` — `OrderIntent` hand-off type.
- Create: `app/services/brokers/kis/mock_scalping_ws/supervisor.py` — event loop: candle close → re-evaluate signal → freshness + debounce gates → emit `TriggerEvent`.
- Create: `app/services/brokers/kis/mock_scalping_ws/config.py` — env gates (see below).
- AST import-guard test: `mock_scalping_ws/` may not import the executor/ledger.

**PR4 (architected):**
- Create: `app/services/brokers/kis/mock_scalping_exec/executor.py` — `execute_monitored()`: mock buy (confirm gate) → poll fill → track price via `MarketState` → TP/SL/time-stop → aggressive-limit exit (uses `ScalpingExitContext`) → failsafe close. Conservative trigger prices (long exit on bid).
- Create: `app/services/brokers/kis/mock_scalping_exec/ws_bridge.py` — consume `TriggerEvent`, concurrency guard (per-symbol in-flight lock + global semaphore cap=max-open-positions), build `OrderIntent` + market-conditions snapshot, call executor with confirm gate.
- Create: `app/services/kis_mock_round_trip_reconciler.py` — round-trip-aware close that pairs entry-fill ↔ exit-fill from **order execution evidence** (not holdings delta), records gross/net PnL + fees + entry/exit reason; holdings snapshot is corroboration only.
- Modify: `KISMockOrderLedger` / `app/services/kis_mock_lifecycle_service.py` — add round-trip linkage (correlation/strategy id, entry/exit reason, fees, gross/net PnL) and a terminal `closed`/`reconciled` state for same-session round trips.
- Create: `scripts/kis_mock_scalping_smoke.py` — default-disabled CLI: dry-run/check-only, small mock run, post-run ledger/position/pending verification.
- Create: `docs/runbooks/kis-mock-scalping-smoke.md` — operator smoke runbook.
- Modify: `/invest` — minimal read-only hook surfacing current mock scalping session state / latest round-trip (UI polish deferred to a separate issue).

**Env gates (introduced across PR1→PR4, all default `False`):**
- `KIS_MOCK_SCALPING_ENABLED` (PR1) — master gate; also required to construct `ScalpingExitContext`.
- `KIS_MOCK_SCALPING_WS_ENABLED` (PR2/3) — daemon/socket gate.
- `KIS_MOCK_SCALPING_WS_CONFIRM` (PR4) — order-mutation gate; without it the bridge dry-runs (preview, no order).

---

## PR1 — Mock scalping sell-guard separation

**Why first:** Lowest risk, zero WebSocket/order surface, fully unit-testable, and it unblocks PR4's exit path. Produces working, tested software on its own.

### Task 1: Add the feature flag

**Files:**
- Modify: `app/core/config.py:175-180` (the `kis_mock_*` settings block)
- Test: `tests/test_kis_mock_scalping_sell_guard.py`

- [ ] **Step 1: Add the setting**

In `app/core/config.py`, in the `kis_mock_*` block (currently ending at `kis_mock_access_token` on line 180), add:

```python
    kis_mock_scalping_enabled: bool = False
```

- [ ] **Step 2: Write a test asserting the default is off**

Create `tests/test_kis_mock_scalping_sell_guard.py`:

```python
"""Unit tests for KIS mock scalping sell-guard separation (ROB-321 PR1)."""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.mark.unit
def test_kis_mock_scalping_disabled_by_default() -> None:
    assert settings.kis_mock_scalping_enabled is False
```

- [ ] **Step 3: Run it**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/core/config.py tests/test_kis_mock_scalping_sell_guard.py
git commit -m "feat(rob-321): add KIS_MOCK_SCALPING_ENABLED flag (default off)"
```

---

### Task 2: Extract the sell-price guard into a pure function

The floor + current-price guard is currently duplicated in `_preview_sell` (`order_validation.py:465-485`) and `_validate_sell_side` (`order_validation.py:653-681`). Extract the decision into one pure function so the bypass matrix is single-sourced and trivially testable.

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py` (add types + pure fn after `DefensiveTrimContext`, around line 62)
- Test: `tests/test_kis_mock_scalping_sell_guard.py`

- [ ] **Step 1: Write the failing tests for the guard matrix**

Append to `tests/test_kis_mock_scalping_sell_guard.py`:

```python
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    ScalpingExitContext,
    evaluate_sell_price_guards,
)
import datetime


def _trim_ctx() -> DefensiveTrimContext:
    return DefensiveTrimContext(
        approval_issue_id="ROB-1",
        requester_agent_id="agent",
        approval_verified_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )


def _scalp_ctx() -> ScalpingExitContext:
    return ScalpingExitContext(strategy_id="kis-mock-v1", reason="stop_loss")


@pytest.mark.unit
def test_guard_blocks_below_floor_when_no_context() -> None:
    # price below avg*1.01 -> floor error
    err = evaluate_sell_price_guards(
        price=1000.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_guard_blocks_below_current_price_when_no_context() -> None:
    # price >= floor (avg low) but below current -> current-price error
    err = evaluate_sell_price_guards(
        price=1000.0, current_price=1100.0, avg_price=900.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_defensive_trim_bypasses_floor_but_not_current_price() -> None:
    # below floor: allowed by trim; but also below current -> still blocked
    err = evaluate_sell_price_guards(
        price=950.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=_trim_ctx(), scalping_exit_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_scalping_exit_bypasses_both_guards() -> None:
    # below floor AND below current: scalping exit allows it (stop-loss)
    err = evaluate_sell_price_guards(
        price=950.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=_scalp_ctx(),
    )
    assert err is None


@pytest.mark.unit
def test_no_context_clean_price_passes() -> None:
    err = evaluate_sell_price_guards(
        price=1100.0, current_price=1050.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v`
Expected: FAIL — `ImportError: cannot import name 'ScalpingExitContext'` / `evaluate_sell_price_guards`.

- [ ] **Step 3: Implement the type + pure function**

In `app/mcp_server/tooling/order_validation.py`, immediately after the `DefensiveTrimContext` dataclass (after line 62), add:

```python
@dataclass(frozen=True)
class ScalpingExitContext:
    """Mock-only authorization to sell a scalping position below both the
    avg*1.01 floor and the current-price guard (stop-loss / take-profit /
    time-stop). Only constructible for is_mock=True orders gated by
    KIS_MOCK_SCALPING_ENABLED. Never threaded from any live/generic path.
    """

    strategy_id: str
    reason: str  # "stop_loss" | "take_profit" | "time_stop"


def evaluate_sell_price_guards(
    *,
    price: float,
    current_price: float,
    avg_price: float,
    defensive_trim_ctx: "DefensiveTrimContext | None",
    scalping_exit_ctx: "ScalpingExitContext | None",
) -> str | None:
    """Single source of truth for limit-sell price guards.

    Returns an error message if the price violates a guard, else None.

    Matrix:
      - scalping_exit_ctx present  -> both guards bypassed (mock scalping exit).
      - defensive_trim_ctx present -> floor bypassed, current-price guard enforced.
      - neither                    -> both guards enforced.
    """
    if scalping_exit_ctx is not None:
        return None
    min_sell_price = avg_price * 1.01
    if price < min_sell_price and defensive_trim_ctx is None:
        return (
            f"Sell price {price} below minimum "
            f"(avg_buy_price * 1.01 = {min_sell_price:.0f})"
        )
    if price < current_price:
        return f"Sell price {price} below current price {current_price}"
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py tests/test_kis_mock_scalping_sell_guard.py
git commit -m "feat(rob-321): extract pure sell-price guard with scalping-exit bypass matrix"
```

---

### Task 3: Add the fail-closed context resolver

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py` (add `_resolve_scalping_exit_context` near `_validate_defensive_trim_preconditions`, ~line 139)
- Test: `tests/test_kis_mock_scalping_sell_guard.py`

- [ ] **Step 1: Write failing tests for the resolver**

Append to `tests/test_kis_mock_scalping_sell_guard.py`:

```python
from app.mcp_server.tooling.order_validation import _resolve_scalping_exit_context


@pytest.mark.unit
def test_resolver_returns_none_when_not_requested(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=False, strategy_id="s", reason="stop_loss",
        side="sell", order_type="limit", is_mock=True,
    )
    assert ctx is None


@pytest.mark.unit
def test_resolver_fail_closed_on_live(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="kis_mock"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="limit", is_mock=False,
        )


@pytest.mark.unit
def test_resolver_fail_closed_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", False, raising=False)
    with pytest.raises(ValueError, match="KIS_MOCK_SCALPING_ENABLED"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="limit", is_mock=True,
        )


@pytest.mark.unit
def test_resolver_returns_context_when_authorized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=True, strategy_id="kis-mock-v1", reason="stop_loss",
        side="sell", order_type="limit", is_mock=True,
    )
    assert ctx is not None and ctx.strategy_id == "kis-mock-v1"
    assert ctx.reason == "stop_loss"


@pytest.mark.unit
def test_resolver_rejects_buy_and_market_and_bad_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="side='sell'"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="buy", order_type="limit", is_mock=True,
        )
    with pytest.raises(ValueError, match="order_type='limit'"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="market", is_mock=True,
        )
    with pytest.raises(ValueError, match="reason"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="moon",
            side="sell", order_type="limit", is_mock=True,
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v -k resolver`
Expected: FAIL — `cannot import name '_resolve_scalping_exit_context'`.

- [ ] **Step 3: Implement the resolver**

In `app/mcp_server/tooling/order_validation.py`, after `_validate_defensive_trim_preconditions` (after line ~192), add:

```python
_SCALPING_EXIT_REASONS = frozenset({"stop_loss", "take_profit", "time_stop"})


def _resolve_scalping_exit_context(
    *,
    scalping_exit: bool,
    strategy_id: str | None,
    reason: str | None,
    side: str,
    order_type: str,
    is_mock: bool,
) -> ScalpingExitContext | None:
    """Fail-closed resolution of a mock scalping exit authorization.

    Returns None when not requested. Raises ValueError on any condition that
    would let a live/generic order acquire the bypass.
    """
    if not scalping_exit:
        return None
    if not settings.kis_mock_scalping_enabled:
        raise ValueError(
            "scalping_exit requires KIS_MOCK_SCALPING_ENABLED=true"
        )
    if not is_mock:
        raise ValueError("scalping_exit is only available for kis_mock orders")
    if side != "sell":
        raise ValueError("scalping_exit requires side='sell'")
    if order_type != "limit":
        raise ValueError("scalping_exit requires order_type='limit'")
    if not strategy_id:
        raise ValueError("scalping_exit requires strategy_id")
    resolved_reason = reason or "stop_loss"
    if resolved_reason not in _SCALPING_EXIT_REASONS:
        raise ValueError(f"invalid scalping_exit reason: {resolved_reason}")
    return ScalpingExitContext(strategy_id=strategy_id, reason=resolved_reason)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v -k resolver`
Expected: PASS (5 resolver tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py tests/test_kis_mock_scalping_sell_guard.py
git commit -m "feat(rob-321): fail-closed scalping-exit context resolver (mock + flag gated)"
```

---

### Task 4: Route `_preview_sell` and `_validate_sell_side` through the pure function

**Files:**
- Modify: `app/mcp_server/tooling/order_validation.py:432-502` (`_preview_sell`) and `:590-683` (`_validate_sell_side`)
- Test: `tests/test_kis_mock_scalping_sell_guard.py`

- [ ] **Step 1: Add a bypass-logging helper**

In `app/mcp_server/tooling/order_validation.py`, after `_log_defensive_trim_bypass` (after line ~138), add:

```python
def _log_scalping_exit_bypass(
    *,
    symbol: str,
    market_type: str,
    price: float,
    current_price: float,
    avg_price: float,
    scalping_exit_ctx: ScalpingExitContext,
    phase: str,
) -> None:
    logger.warning(
        "kis_mock_scalping_exit_bypass: sell guards bypassed",
        extra={
            "account_mode": "kis_mock",
            "symbol": symbol,
            "market_type": market_type,
            "price": price,
            "current_price": current_price,
            "avg_price": avg_price,
            "strategy_id": scalping_exit_ctx.strategy_id,
            "reason": scalping_exit_ctx.reason,
            "phase": phase,
        },
    )
```

- [ ] **Step 2: Write failing wiring tests (preview + validate, live unaffected)**

Append to `tests/test_kis_mock_scalping_sell_guard.py`:

```python
from unittest.mock import AsyncMock
from app.mcp_server.tooling import order_validation


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_scalping_exit_allows_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation, "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    result = await order_validation._preview_sell(
        symbol="005930", order_type="limit", quantity=10,
        price=950.0, current_price=980.0, market_type="kr",
        scalping_exit_ctx=ScalpingExitContext(strategy_id="s", reason="stop_loss"),
        is_mock=True,
    )
    assert "error" not in result
    assert result["price"] == 950.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_still_blocks_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation, "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    # No scalping ctx, no trim ctx: live behavior preserved.
    result = await order_validation._preview_sell(
        symbol="005930", order_type="limit", quantity=10,
        price=950.0, current_price=980.0, market_type="kr",
        is_mock=False,
    )
    assert "error" in result and "below minimum" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_scalping_exit_allows_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation, "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="005930", normalized_symbol="005930", market_type="kr",
        quantity=10, order_type="limit", price=950.0, current_price=980.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        scalping_exit_ctx=ScalpingExitContext(strategy_id="s", reason="stop_loss"),
        is_mock=True, dry_run=True,
    )
    assert err is None and errors == []
    assert avg == 1000.0
```

> Note: confirm `_validate_sell_side`'s holdings/exposure source against the merged code; if it reads exposure separately from `_get_holdings_for_order`, patch that source too. Keep the assertion (no error / err is None) stable.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v -k "preview or validate_sell"`
Expected: FAIL — `_preview_sell`/`_validate_sell_side` got unexpected keyword `scalping_exit_ctx`.

- [ ] **Step 4: Thread the param and call the pure function in `_preview_sell`**

In `_preview_sell` (signature at line 432), add the parameter after `defensive_trim_ctx`:

```python
    scalping_exit_ctx: ScalpingExitContext | None = None,
```

Replace the limit-branch guard block (current lines 465-485) with:

```python
        guard_error = evaluate_sell_price_guards(
            price=price,
            current_price=current_price,
            avg_price=avg_price,
            defensive_trim_ctx=defensive_trim_ctx,
            scalping_exit_ctx=scalping_exit_ctx,
        )
        if guard_error is not None:
            result["error"] = guard_error
            return result
        if scalping_exit_ctx is not None and price < avg_price * 1.01:
            _log_scalping_exit_bypass(
                symbol=symbol, market_type=market_type, price=price,
                current_price=current_price, avg_price=avg_price,
                scalping_exit_ctx=scalping_exit_ctx, phase="preview",
            )
        elif price < avg_price * 1.01 and defensive_trim_ctx is not None:
            _log_defensive_trim_bypass(
                symbol=symbol, market_type=market_type, price=price,
                current_price=current_price, avg_price=avg_price,
                min_sell_price=avg_price * 1.01,
                defensive_trim_ctx=defensive_trim_ctx, phase="preview",
            )
        order_quantity = holdings["quantity"] if quantity is None else quantity
        execution_price = price
        result["price"] = execution_price
```

- [ ] **Step 5: Thread the param and call the pure function in `_validate_sell_side`**

In `_validate_sell_side` (signature at line 590), add after `defensive_trim_ctx`:

```python
    scalping_exit_ctx: ScalpingExitContext | None = None,
```

Replace the limit-branch guard block (current lines 652-681) with:

```python
    if order_type == "limit" and price is not None:
        guard_error = evaluate_sell_price_guards(
            price=price,
            current_price=current_price,
            avg_price=avg_price,
            defensive_trim_ctx=defensive_trim_ctx,
            scalping_exit_ctx=scalping_exit_ctx,
        )
        if guard_error is not None:
            return 0.0, 0.0, order_error_fn(guard_error)
        if scalping_exit_ctx is not None and price < avg_price * 1.01:
            _log_scalping_exit_bypass(
                symbol=normalized_symbol, market_type=market_type, price=price,
                current_price=current_price, avg_price=avg_price,
                scalping_exit_ctx=scalping_exit_ctx, phase="execution",
            )
        elif price < avg_price * 1.01 and defensive_trim_ctx is not None:
            _log_defensive_trim_bypass(
                symbol=normalized_symbol, market_type=market_type, price=price,
                current_price=current_price, avg_price=avg_price,
                min_sell_price=avg_price * 1.01,
                defensive_trim_ctx=defensive_trim_ctx, phase="execution",
            )
```

- [ ] **Step 6: Run the full PR1 test file**

Run: `uv run pytest tests/test_kis_mock_scalping_sell_guard.py -v`
Expected: PASS (all tests).

- [ ] **Step 7: Run the existing defensive-trim regression to prove no behavior change**

Run: `uv run pytest tests/test_mcp_place_order_defensive_trim.py -v`
Expected: PASS (unchanged — defensive_trim still bypasses floor, still enforces current-price guard).

- [ ] **Step 8: Commit**

```bash
git add app/mcp_server/tooling/order_validation.py tests/test_kis_mock_scalping_sell_guard.py
git commit -m "feat(rob-321): route sell preview/validate through pure guard; scalping-exit bypass"
```

---

### Task 5: PR1 verification gate

- [ ] **Step 1: Lint + import guards + targeted tests (per `feedback_premerge_full_ci_gate`)**

```bash
uv run ruff check app/ tests/
uv run pytest tests/test_kis_mock_scalping_sell_guard.py tests/test_mcp_place_order_defensive_trim.py -v
```
Expected: ruff clean; all tests PASS.

- [ ] **Step 2: Confirm live path untouched — grep the two out-of-scope filters are unchanged**

```bash
git diff --name-only origin/main -- app/services/kis_trading_service.py app/services/order_service.py
```
Expected: **empty** (neither file modified — live/auto-trade safety preserved).

- [ ] **Step 3: Open PR (base `main`), confirm full Test workflow green before merge.**

---

## PR2 — Live KIS quote/orderbook WebSocket (read-only) + host separation

**Goal:** A read-only market-data feed; zero order surface.

**Key interfaces (lock these):**
- `QuoteTick(symbol, last_price, ts)`, `OrderBookSnapshot(symbol, bid, ask, bid_qty, ask_qty, ts)` in `quote_parsers.py`.
- `MarketState.update_from_tick(...)`, `MarketState.update_from_book(...)`, `MarketState.spread_bps()`, `MarketState.age_seconds(now)`.
- Market-stream client exposes `async listen(on_tick, on_book)` mirroring `KISExecutionWebSocket.listen`.

**Test strategy:**
- Fake WS server feeds canned quote/orderbook frames → assert parser output and `MarketState` transitions.
- Reconnect/backoff/heartbeat-timeout tests reuse the execution-WS test harness shape.
- **Host fail-closed test:** constructing the market-stream client against the mock order host (or any non-quote host) raises; an order mutation can never be issued on the quote transport.

**Detailed step-level plan:** author `docs/plans/ROB-321-pr2-quote-ws.md` after reading `kis_websocket_internal/{client,protocol,parsers}.py` against merged main.

---

## PR3 — Strategy/risk contract + supervisor (default-off, dry-run)

**Goal:** Deterministic signal + risk envelope + trigger emission. No orders.

**Key interfaces (lock these — mirror `binance/demo_scalping`):**
- `SignalConfig`, `SignalDecision(has_entry, side, entry_price, tp_price, sl_price, confidence, reason_codes)`, `evaluate_signal(candles, config) -> SignalDecision` (pure).
- `ScalpingRiskLimits` (KIS allowlist, max_notional, max_open_positions, per_symbol_cooldown_s, daily_attempt_cap, daily_loss_budget, spread_bps_guard, data_freshness_s), `ReasonCode` constants, `evaluate_risk(...) -> RiskDecision` (accumulate all blocking codes, no short-circuit).
- `OrderIntent(symbol, side, order_type, target_notional, entry_reference_price, tp_price, sl_price, strategy_id, reason_codes, source_candle_close_ms, evaluated_at_ms)`.
- `TriggerEvent` emitted by `supervisor` only when freshness + per-symbol debounce pass.
- AST import-guard test: `mock_scalping_ws/` must not import `mock_scalping_exec/` or the ledger.

**Test strategy:** pure-function tables for `evaluate_signal` and `evaluate_risk` (each ReasonCode); supervisor fed a synthetic candle stream asserts a single `TriggerEvent` per valid setup and debounce suppression.

**Detailed step-level plan:** author `docs/plans/ROB-321-pr3-signal-supervisor.md`.

---

## PR4 — Execution bridge + round-trip ledger/reconcile + smoke runbook

**Goal:** The only mutation path; mock-only orders; round-trip close.

**Key design (lock these):**
- `executor.execute_monitored(intent, *, confirm)`: mock buy via existing `_place_order_impl` path → poll fill → track price via `MarketState` → on TP/SL/time-stop submit aggressive-limit exit carrying a `ScalpingExitContext(strategy_id, reason)` (PR1) → failsafe close on timeout. Long exits trigger on **bid** (conservative). Without `confirm`/`KIS_MOCK_SCALPING_WS_CONFIRM`, dry-run preview only.
- `ws_bridge`: per-symbol in-flight lock + global semaphore (cap = `max_open_positions`) + DB ledger re-check before executor; builds `OrderIntent` + market-conditions snapshot.
- **Round-trip reconcile (the ROB-316/§2-4 fix):** pair entry-fill ↔ exit-fill from **order execution evidence** (submit response / execution WS), not holdings delta. Record gross/net PnL, entry+exit fees, entry/exit reason; reach a terminal `closed`/`reconciled` state even when the holdings snapshot never showed the intermediate position. Holdings snapshot is corroboration only — a missing intermediate position is **not** an anomaly for a same-session round trip.
- Ledger linkage columns on `KISMockOrderLedger`: `strategy_id`, `correlation_id`, `entry_exit_reason`, `fees`, `gross_pnl`, `net_pnl` (additive Alembic migration; operator runs `alembic upgrade head` separately — not auto-applied).

**Test strategy (covers Acceptance criteria 3–7):**
- Fake WS + fake broker: tick → entry signal → mock buy → filled → TP and SL exit paths both close round-trip as `reconciled`/`closed` (not `anomaly`), including the fast round-trip where holdings never reflect the intermediate position.
- Flag-off (`KIS_MOCK_SCALPING_ENABLED`/`WS_ENABLED` false) → daemon submits no order.
- Risk gates tested individually: max notional, max open positions, cooldown, daily caps, kill switch.
- Host fail-closed: order path can only hit the mock host.

**Runbook:** `docs/runbooks/kis-mock-scalping-smoke.md` — dry-run/check-only → small mock run → post-run ledger/position/pending verification (mirror `docs/runbooks/binance-futures-demo-smoke.md`).

**Detailed step-level plan:** author `docs/plans/ROB-321-pr4-exec-bridge.md`.

---

## Self-Review

**Spec coverage (ROB-321 acceptance criteria):**
1. Mock scalping stop-loss below avg passes validation → **PR1 Task 4** (`test_*_scalping_exit_allows_below_floor`). ✅
2. Live/generic sell guards unchanged → **PR1 Task 4 + Task 5 Step 2** (live test + diff-empty check). ✅
3. WS fake/integration tick → entry → buy → filled → TP/SL sell → **PR4 test strategy**. ✅ (planned)
4. Round-trip ledger closed/reconciled, not anomaly → **PR4 round-trip reconciler** (explicitly handles the fast round-trip). ✅ (planned)
5. Flag-off submits no order → **PR4 test strategy** (+ PR1 flag default). ✅ (planned)
6. max notional / max open positions / cooldown / kill switch tested → **PR3 risk contract + PR4 tests**. ✅ (planned)
7. Operator smoke runbook → **PR4 `docs/runbooks/kis-mock-scalping-smoke.md`**. ✅ (planned)

Scope items §1 (guard separation) and §6 (feature flag, account_mode logging, no live mixing) are fully realized in PR1; §2 (WS) PR2; §3 (entry contract) PR3; §4 (exit manager) + §5 (ledger/reconcile) PR4.

**Placeholder scan:** PR1 steps contain complete code/commands. PR2–4 are explicitly scoped subsystem outlines (locked interfaces + test strategy) with their own detailed plans deferred — not in-task placeholders.

**Type consistency:** `ScalpingExitContext(strategy_id, reason)` and `evaluate_sell_price_guards(price, current_price, avg_price, defensive_trim_ctx, scalping_exit_ctx)` and `_resolve_scalping_exit_context(scalping_exit, strategy_id, reason, side, order_type, is_mock)` are used consistently across PR1 Tasks 2–4 and referenced by PR4's executor.

**Open verification item for the implementer (PR1 Task 4 Step 2 note):** confirm `_validate_sell_side`'s holdings/exposure read path against merged main and patch the correct source in the test; the assertion (no error) stays fixed.
