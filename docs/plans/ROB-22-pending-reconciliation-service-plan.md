# ROB-22 — Pending Reconciliation Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear issue:** ROB-22 — [Foundation] Add pending reconciliation service for Research Run live refresh

**Goal:** Add a pure, read-only service that converts raw broker pending orders + market context into decision-support objects classified as `maintain` / `near_fill` / `too_far` / `chasing_risk` / `data_mismatch` / `kr_pending_non_nxt` / `unknown_venue` (plus an `unknown` fallback), with explicit warnings on missing/stale context. Reusable by both Research Run live refresh and Decision Session proposal generation.

**Architecture:**
- One new module `app/services/pending_reconciliation_service.py` containing only pure dataclasses + pure functions: no broker / DB / HTTP / Redis / TaskIQ imports, no broker mutation.
- Callers (Research Run live refresh, Decision Session proposal generation) collect their own context (pending orders from `get_order_history_impl(status="pending", market=...)`, quotes via `app.services.market_data.get_quote`, orderbook via `app.services.market_data.get_orderbook`, support/resistance via `app.mcp_server.tooling.fundamentals._support_resistance.get_support_resistance_impl`, NXT eligibility via `KrSymbolUniverseService.is_nxt_eligible`) and pass it as plain DTOs into the service.
- The service does not mutate state, never calls `place_order` / `modify_order` / `cancel_order` / `manage_watch_alerts` / paper trade APIs, and never writes to the DB. It returns dataclasses.

**Tech stack:** Python 3.13, `dataclasses`, `typing`, `decimal.Decimal`, `pytest`. No new third-party dependencies.

**ROB-20 boundary:** ROB-20 (live refresh wiring + UI) is out of scope. If a behavior here genuinely requires ROB-20-side wiring, stop and report it as a blocker. This PR adds only the pure service + tests.

**Trading-safety guardrails (non-negotiable):**
- Read-only / decision-support only. No `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, paper/dry-run/live order placement, watch registration, or order-intent creation introduced.
- The new module must not transitively import broker / order-execution / watch-alert / paper-order / fill-notification / KIS-websocket modules. Enforced by a subprocess `sys.modules` test (Task 8) modeled on `tests/services/test_operator_decision_session_safety.py`.
- Decision Session creation = ledger persistence only; not execution approval. TradingAgents output, if present in inputs, must remain `advisory_only=true / execution_allowed=false`.

---

## File Structure

| Path | Status | Responsibility |
|------|--------|----------------|
| `app/services/pending_reconciliation_service.py` | create | Pure dataclasses + pure classification functions. No I/O. |
| `tests/services/test_pending_reconciliation_service.py` | create | Unit tests for every classification, warning, threshold, and configuration override. |
| `tests/services/test_pending_reconciliation_service_safety.py` | create | Subprocess `sys.modules` test that asserts the service module does not transitively import broker / order-execution / watch-alert / paper / fill-notification / DB / Redis modules. |
| `docs/plans/ROB-22-pending-reconciliation-service-plan.md` | create (this file) | Implementation plan. |

No changes to:
- `app/mcp_server/tooling/orders_*` (read inputs already exist via `get_order_history_impl`).
- `app/services/trading_decision_service.py`, `app/services/operator_decision_session_service.py`, `app/services/tradingagents_research_service.py` (they will *consume* this service in a later issue/PR; ROB-22 only delivers the pure service).
- `app/models/*`, `app/schemas/*`, `alembic/*`, routers, Prefect flows, UI templates.

---

## Domain Reference (read once before coding)

- **KR pending orders source.** `app.mcp_server.tooling.orders_history.get_order_history_impl(status="pending", market="kr")` returns *all* KR domestic broker pending orders (KIS `inquire_korea_orders`). It is **not** an NXT-only list. The normalized order shape is `_normalize_kis_domestic_order(...)` from `app/mcp_server/tooling/orders_modify_cancel.py:127` and includes:
  - `order_id`, `symbol` (6-digit KR code), `side` (`buy`/`sell`), `status`, `ordered_qty`, `filled_qty`, `remaining_qty`, `ordered_price`, `filled_avg_price`, `ordered_at`, `filled_at`, `currency` (`"KRW"`).
- **NXT eligibility source.** `kr_symbol_universe.nxt_eligible` (`app/models/kr_symbol_universe.py:23`). Helper: `app.services.kr_symbol_universe_service.is_nxt_eligible(symbol)` (`app/services/kr_symbol_universe_service.py:363`). Default to `False` when the symbol is missing or inactive.
- **Important non-NXT example.** `034220` (LG디스플레이) appears in `inquire_korea_orders` but has `nxt_eligible=False`. It must classify as `kr_pending_non_nxt` and surface `nxt_actionable=False` plus a `non_nxt_venue` warning. We must not reject these orders — they are valid KIS standard-routing pending orders.
- **Quote shape.** `app/services/market_data/contracts.py` — `Quote(symbol, market, price, source, previous_close, open, high, low, volume, value)`. `Quote` itself has no `as_of` field; the reconciliation service receives an explicit caller-supplied `quote_as_of: datetime | None` next to the quote so callers (live refresh vs. proposal-generation) can decide what counts as "fresh."
- **Orderbook shape.** `OrderbookSnapshot(asks, bids, total_ask_qty, total_bid_qty, bid_ask_ratio, ...)` from the same module. `OrderbookLevel(price, quantity)`.
- **Support/resistance shape.** `get_support_resistance_impl` returns `{"symbol", "current_price", "supports": [...], "resistances": [...]}` where each level has `price`, `distance_pct` (% from current price), `strength`, `sources`. On error it returns an `_error_payload(...)` dict; callers should treat that as missing context.

The reconciliation service should not import any of the above modules; it accepts already-shaped DTOs and treats every field as optional.

---

## Public API of the Service

```python
# app/services/pending_reconciliation_service.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

Classification = Literal[
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
]
Market = Literal["kr", "us", "crypto"]
Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class PendingOrderInput:
    order_id: str
    symbol: str
    market: str          # raw caller-provided value; service validates
    side: str            # raw caller-provided value; service validates
    ordered_price: Decimal
    ordered_qty: Decimal
    remaining_qty: Decimal
    currency: str | None
    ordered_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteContext:
    price: Decimal
    as_of: datetime | None        # caller-supplied freshness timestamp


@dataclass(frozen=True, slots=True)
class OrderbookLevelContext:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookContext:
    best_bid: OrderbookLevelContext | None
    best_ask: OrderbookLevelContext | None
    total_bid_qty: Decimal | None = None
    total_ask_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SupportResistanceLevel:
    price: Decimal
    distance_pct: Decimal


@dataclass(frozen=True, slots=True)
class SupportResistanceContext:
    nearest_support: SupportResistanceLevel | None
    nearest_resistance: SupportResistanceLevel | None


@dataclass(frozen=True, slots=True)
class KrUniverseContext:
    nxt_eligible: bool
    name: str | None = None
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class MarketContextInput:
    quote: QuoteContext | None
    orderbook: OrderbookContext | None
    support_resistance: SupportResistanceContext | None
    kr_universe: KrUniverseContext | None  # required when market == "kr"


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    near_fill_pct: Decimal = Decimal("0.5")
    too_far_pct: Decimal = Decimal("5.0")
    chasing_pct: Decimal = Decimal("3.0")
    chasing_resistance_pct: Decimal = Decimal("1.0")
    chasing_support_pct: Decimal = Decimal("1.0")
    quote_stale_seconds: int = 300


@dataclass(frozen=True, slots=True)
class PendingReconciliationItem:
    order_id: str
    symbol: str
    market: str
    side: str
    classification: Classification
    nxt_actionable: bool | None  # None when market != "kr" or when KR universe is missing
    gap_pct: Decimal | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


def reconcile_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> PendingReconciliationItem: ...


def reconcile_pending_orders(
    orders: Sequence[PendingOrderInput],
    contexts_by_order_id: dict[str, MarketContextInput],
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> list[PendingReconciliationItem]: ...
```

**Classification rules (executed in this order; first match wins, except warnings always accumulate):**

1. **`unknown_venue`** if `order.market not in {"kr","us","crypto"}` *or* `order.side not in {"buy","sell"}`. Warnings: `unknown_venue`, optionally `unknown_side`.
2. **`data_mismatch`** if obvious cross-field contradictions:
   - `order.market == "kr"` and `order.currency` is set and not `"KRW"`.
   - `order.market == "us"` and `order.currency` is set and not `"USD"`.
   - `order.market == "crypto"` and `order.currency` is set and not in `{"KRW","USDT"}`.
   - `order.ordered_price <= 0` or `order.remaining_qty <= 0`.
3. **`kr_pending_non_nxt`** if `order.market == "kr"` and `context.kr_universe is not None` and `context.kr_universe.nxt_eligible is False`. Set `nxt_actionable=False`. Warnings: `non_nxt_venue`. Continue to compute `gap_pct` and `decision_support` for transparency, but do not change the top-level classification.
4. If `context.kr_universe is None` and `order.market == "kr"`: emit warning `missing_kr_universe`; `nxt_actionable=None`.
5. If `context.quote is None`: emit warning `missing_quote`; `gap_pct=None`; classification falls through to `unknown` unless rule 1, 2, or 3 already won.
6. If `context.quote.as_of` is older than `config.quote_stale_seconds` relative to `now` (or older than the order's `ordered_at` when `now` is `None`): emit warning `stale_quote`; still attempt classification using the (possibly stale) price.
7. If `context.orderbook is None`: emit warning `missing_orderbook`. Not blocking.
8. If `context.support_resistance is None`: emit warning `missing_support_resistance`. Not blocking.
9. Compute `gap_pct = (quote.price - order.ordered_price) / order.ordered_price * 100` (signed; always quote-minus-order).
   - For `side == "buy"`: positive `gap_pct` means the market trades above the buy limit (buy unlikely to fill); negative means market is at or below the limit (likely to fill soon).
   - For `side == "sell"`: positive `gap_pct` means market is above the sell limit (sell likely to fill); negative means below (sell unlikely to fill).
   - Define `signed_distance_to_fill = -gap_pct if side=="buy" else gap_pct`. `signed_distance_to_fill <= 0` means the order is at-or-through the market.
10. **`near_fill`** if `abs(gap_pct) <= near_fill_pct`. Reason: `gap_within_near_fill_pct`.
11. **`too_far`** if `signed_distance_to_fill < 0` and `abs(gap_pct) >= too_far_pct`. Reason: `gap_against_fill_exceeds_too_far_pct`. (E.g., a buy order whose limit is 5% above the current ask — it would fill immediately if it were live, suggesting it is mispriced or stale.)
12. **`chasing_risk`** if `signed_distance_to_fill > chasing_pct` (order is "chasing" the market away from fill) **and** support/resistance context indicates the price has run away:
    - For `side == "buy"`: `nearest_resistance` exists and `nearest_resistance.distance_pct <= chasing_resistance_pct` (price near resistance while a buy is left behind below).
    - For `side == "sell"`: `nearest_support` exists and `nearest_support.distance_pct <= chasing_support_pct` (price near support while a sell is left behind above).
    Reason: `price_diverged_into_<resistance|support>`.
13. **`maintain`** as the default when none of the above match.
14. If quote was missing (rule 5) and rules 1/2/3 did not classify, classification stays `unknown`.

`decision_support` always includes (even when `None`):
- `current_price`
- `gap_pct`
- `signed_distance_to_fill`
- `nearest_support_price`, `nearest_support_distance_pct`
- `nearest_resistance_price`, `nearest_resistance_distance_pct`
- `bid_ask_spread_pct` (when both `best_bid` and `best_ask` are present)

**`nxt_actionable` semantics:**
- `market == "kr"` and `kr_universe.nxt_eligible == True` → `True`.
- `market == "kr"` and `kr_universe.nxt_eligible == False` → `False` (combined with classification `kr_pending_non_nxt`).
- `market == "kr"` and `kr_universe is None` → `None` (with warning `missing_kr_universe`).
- `market != "kr"` → `None` (NXT is a KR-only routing concept).

---

## Self-Review

### Spec coverage

- **AC: pure service with unit tests, no broker/order side effects.** Tasks 1–7 (pure dataclasses + classifier + tests). Task 8 enforces import isolation in a subprocess. No new code touches broker / order / paper / watch modules.
- **AC: classifies maintain, near_fill, too_far, chasing_risk, data_mismatch, kr_pending_non_nxt, unknown_venue.** Each has a dedicated test in Task 7 (and rule-by-rule in Tasks 4–6). Plus an `unknown` fallback for the missing-quote, unknown-venue-already-handled-but-no-quote case.
- **AC: handles missing quote/orderbook/support-resistance gracefully with explicit missing/stale warnings.** Warnings: `missing_quote`, `stale_quote`, `missing_orderbook`, `missing_support_resistance`, `missing_kr_universe`. Tested in Task 6.
- **AC: reusable by Research Run live refresh and Decision Session proposal generation.** The service exposes pure functions over plain dataclasses with no caller-specific assumptions. Task 7 includes a "two-callers" test that exercises the function with two different context-construction styles (one mimicking research-run live refresh, one mimicking decision-session proposal-generation) without touching their actual modules.

### Placeholder scan
No "TBD" / "implement later" / "similar to Task N" present. Each task carries the actual code/tests it expects.

### Type consistency
`Classification`, `Market`, `Side`, `PendingOrderInput`, `MarketContextInput`, `ReconciliationConfig`, `PendingReconciliationItem` are referenced by the same names in every task. `signed_distance_to_fill` is defined once in rule 9 and reused in rules 11/12.

---

## Tasks

### Task 1 — Plan only (this commit)

**Files:**
- Create: `docs/plans/ROB-22-pending-reconciliation-service-plan.md` (this file)

- [ ] **Step 1: Verify the plan file is on disk and committed.**

Run:
```bash
test -f docs/plans/ROB-22-pending-reconciliation-service-plan.md && \
  git status docs/plans/ROB-22-pending-reconciliation-service-plan.md
```
Expected: file exists; either staged or already committed on the feature branch.

- [ ] **Step 2: Commit the plan.**

```bash
git add docs/plans/ROB-22-pending-reconciliation-service-plan.md
git commit -m "$(cat <<'EOF'
docs(rob-22): plan pure pending reconciliation service

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 2 — Define the dataclass surface (no logic)

**Files:**
- Create: `app/services/pending_reconciliation_service.py`
- Create: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing test for module import + dataclass shape.**

Append to `tests/services/test_pending_reconciliation_service.py`:
```python
"""Unit tests for app.services.pending_reconciliation_service.

These tests cover only the pure classifier + warning logic. They do not
import any broker, DB, Redis, or HTTP module; the service module under
test must not transitively import them either (see
test_pending_reconciliation_service_safety.py).
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest


@pytest.mark.unit
def test_module_exposes_public_api() -> None:
    from app.services import pending_reconciliation_service as svc

    assert hasattr(svc, "PendingOrderInput")
    assert hasattr(svc, "QuoteContext")
    assert hasattr(svc, "OrderbookContext")
    assert hasattr(svc, "OrderbookLevelContext")
    assert hasattr(svc, "SupportResistanceContext")
    assert hasattr(svc, "SupportResistanceLevel")
    assert hasattr(svc, "KrUniverseContext")
    assert hasattr(svc, "MarketContextInput")
    assert hasattr(svc, "ReconciliationConfig")
    assert hasattr(svc, "PendingReconciliationItem")
    assert callable(svc.reconcile_pending_order)
    assert callable(svc.reconcile_pending_orders)


@pytest.mark.unit
def test_item_has_required_fields() -> None:
    from app.services.pending_reconciliation_service import (
        PendingReconciliationItem,
    )

    expected = {
        "order_id",
        "symbol",
        "market",
        "side",
        "classification",
        "nxt_actionable",
        "gap_pct",
        "reasons",
        "warnings",
        "decision_support",
    }
    assert {f.name for f in fields(PendingReconciliationItem)} == expected
```

- [ ] **Step 2: Run tests to verify they fail.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.pending_reconciliation_service'`.

- [ ] **Step 3: Create the dataclass-only module.**

Write `app/services/pending_reconciliation_service.py`:
```python
"""Pure pending-order reconciliation service.

Read-only / decision-support only. This module must not import broker,
order-execution, watch-alert, paper-order, fill-notification, KIS-websocket,
DB, or Redis modules. Callers collect their own context (orders, quotes,
orderbook, support/resistance, KR universe) and pass it as plain DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

Classification = Literal[
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class PendingOrderInput:
    order_id: str
    symbol: str
    market: str
    side: str
    ordered_price: Decimal
    ordered_qty: Decimal
    remaining_qty: Decimal
    currency: str | None
    ordered_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteContext:
    price: Decimal
    as_of: datetime | None


@dataclass(frozen=True, slots=True)
class OrderbookLevelContext:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookContext:
    best_bid: OrderbookLevelContext | None
    best_ask: OrderbookLevelContext | None
    total_bid_qty: Decimal | None = None
    total_ask_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SupportResistanceLevel:
    price: Decimal
    distance_pct: Decimal


@dataclass(frozen=True, slots=True)
class SupportResistanceContext:
    nearest_support: SupportResistanceLevel | None
    nearest_resistance: SupportResistanceLevel | None


@dataclass(frozen=True, slots=True)
class KrUniverseContext:
    nxt_eligible: bool
    name: str | None = None
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class MarketContextInput:
    quote: QuoteContext | None
    orderbook: OrderbookContext | None
    support_resistance: SupportResistanceContext | None
    kr_universe: KrUniverseContext | None


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    near_fill_pct: Decimal = Decimal("0.5")
    too_far_pct: Decimal = Decimal("5.0")
    chasing_pct: Decimal = Decimal("3.0")
    chasing_resistance_pct: Decimal = Decimal("1.0")
    chasing_support_pct: Decimal = Decimal("1.0")
    quote_stale_seconds: int = 300


@dataclass(frozen=True, slots=True)
class PendingReconciliationItem:
    order_id: str
    symbol: str
    market: str
    side: str
    classification: Classification
    nxt_actionable: bool | None
    gap_pct: Decimal | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


def reconcile_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> PendingReconciliationItem:
    raise NotImplementedError


def reconcile_pending_orders(
    orders: Sequence[PendingOrderInput],
    contexts_by_order_id: dict[str, MarketContextInput],
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> list[PendingReconciliationItem]:
    raise NotImplementedError


__all__ = [
    "Classification",
    "PendingOrderInput",
    "QuoteContext",
    "OrderbookLevelContext",
    "OrderbookContext",
    "SupportResistanceLevel",
    "SupportResistanceContext",
    "KrUniverseContext",
    "MarketContextInput",
    "ReconciliationConfig",
    "PendingReconciliationItem",
    "reconcile_pending_order",
    "reconcile_pending_orders",
]
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit.**

```bash
git add app/services/pending_reconciliation_service.py tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
feat(rob-22): add pure pending reconciliation service skeleton

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 3 — Implement venue / data-mismatch / KR-non-NXT classification (rules 1–4)

**Files:**
- Modify: `app/services/pending_reconciliation_service.py`
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing tests for venue + data-mismatch + KR non-NXT.**

Append to the test file:
```python
from decimal import Decimal

from app.services.pending_reconciliation_service import (
    KrUniverseContext,
    MarketContextInput,
    PendingOrderInput,
    QuoteContext,
    reconcile_pending_order,
)


def _empty_context(kr_universe: KrUniverseContext | None = None) -> MarketContextInput:
    return MarketContextInput(
        quote=None,
        orderbook=None,
        support_resistance=None,
        kr_universe=kr_universe,
    )


def _order(**overrides) -> PendingOrderInput:
    base = dict(
        order_id="O1",
        symbol="005930",
        market="kr",
        side="buy",
        ordered_price=Decimal("70000"),
        ordered_qty=Decimal("10"),
        remaining_qty=Decimal("10"),
        currency="KRW",
        ordered_at=None,
    )
    base.update(overrides)
    return PendingOrderInput(**base)


@pytest.mark.unit
def test_unknown_venue_market() -> None:
    item = reconcile_pending_order(_order(market="paper"), _empty_context())
    assert item.classification == "unknown_venue"
    assert "unknown_venue" in item.warnings
    assert item.nxt_actionable is None


@pytest.mark.unit
def test_unknown_venue_side() -> None:
    item = reconcile_pending_order(_order(side="short"), _empty_context())
    assert item.classification == "unknown_venue"
    assert "unknown_side" in item.warnings


@pytest.mark.unit
def test_data_mismatch_currency_kr_usd() -> None:
    item = reconcile_pending_order(_order(currency="USD"), _empty_context())
    assert item.classification == "data_mismatch"
    assert "currency_mismatch" in item.reasons


@pytest.mark.unit
def test_data_mismatch_non_positive_price() -> None:
    item = reconcile_pending_order(
        _order(ordered_price=Decimal("0")), _empty_context()
    )
    assert item.classification == "data_mismatch"
    assert "non_positive_ordered_price" in item.reasons


@pytest.mark.unit
def test_kr_pending_non_nxt() -> None:
    ctx = _empty_context(
        kr_universe=KrUniverseContext(
            nxt_eligible=False, name="LG디스플레이", exchange="KOSPI"
        )
    )
    item = reconcile_pending_order(_order(symbol="034220"), ctx)
    assert item.classification == "kr_pending_non_nxt"
    assert item.nxt_actionable is False
    assert "non_nxt_venue" in item.warnings


@pytest.mark.unit
def test_kr_universe_missing_warning() -> None:
    item = reconcile_pending_order(_order(symbol="034220"), _empty_context())
    assert "missing_kr_universe" in item.warnings
    assert item.nxt_actionable is None


@pytest.mark.unit
def test_kr_nxt_eligible_marks_nxt_actionable_true() -> None:
    ctx = _empty_context(kr_universe=KrUniverseContext(nxt_eligible=True))
    item = reconcile_pending_order(_order(), ctx)
    assert item.nxt_actionable is True
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: 7 new failures (NotImplementedError or assertion error).

- [ ] **Step 3: Implement rules 1–4 in the service.**

Replace the bodies of `reconcile_pending_order` and add internal helpers in `app/services/pending_reconciliation_service.py`:
```python
_VALID_MARKETS = ("kr", "us", "crypto")
_VALID_SIDES = ("buy", "sell")
_CURRENCY_BY_MARKET = {
    "kr": frozenset({"KRW"}),
    "us": frozenset({"USD"}),
    "crypto": frozenset({"KRW", "USDT"}),
}


def _check_unknown_venue(
    order: PendingOrderInput,
    warnings: list[str],
) -> bool:
    bad = False
    if order.market not in _VALID_MARKETS:
        warnings.append("unknown_venue")
        bad = True
    if order.side not in _VALID_SIDES:
        warnings.append("unknown_side")
        bad = True
    return bad


def _check_data_mismatch(
    order: PendingOrderInput,
    reasons: list[str],
) -> bool:
    bad = False
    if order.ordered_price is None or order.ordered_price <= 0:
        reasons.append("non_positive_ordered_price")
        bad = True
    if order.remaining_qty is None or order.remaining_qty <= 0:
        reasons.append("non_positive_remaining_qty")
        bad = True
    if order.currency:
        allowed = _CURRENCY_BY_MARKET.get(order.market)
        if allowed is not None and order.currency.upper() not in allowed:
            reasons.append("currency_mismatch")
            bad = True
    return bad


def _resolve_nxt_actionable(
    order: PendingOrderInput,
    context: MarketContextInput,
    warnings: list[str],
) -> tuple[bool | None, bool]:
    """Return (nxt_actionable, is_kr_pending_non_nxt)."""
    if order.market != "kr":
        return None, False
    if context.kr_universe is None:
        warnings.append("missing_kr_universe")
        return None, False
    if context.kr_universe.nxt_eligible:
        return True, False
    warnings.append("non_nxt_venue")
    return False, True


def _empty_decision_support() -> dict[str, Decimal | str | None]:
    return {
        "current_price": None,
        "gap_pct": None,
        "signed_distance_to_fill": None,
        "nearest_support_price": None,
        "nearest_support_distance_pct": None,
        "nearest_resistance_price": None,
        "nearest_resistance_distance_pct": None,
        "bid_ask_spread_pct": None,
    }


def reconcile_pending_order(  # noqa: C901  (rule-by-rule classifier)
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> PendingReconciliationItem:
    cfg = config or ReconciliationConfig()
    warnings: list[str] = []
    reasons: list[str] = []
    decision_support = _empty_decision_support()

    if _check_unknown_venue(order, warnings):
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="unknown_venue",
            nxt_actionable=None,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if _check_data_mismatch(order, reasons):
        nxt_actionable, _ = _resolve_nxt_actionable(order, context, warnings)
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="data_mismatch",
            nxt_actionable=nxt_actionable,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    nxt_actionable, is_non_nxt = _resolve_nxt_actionable(order, context, warnings)
    if is_non_nxt:
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="kr_pending_non_nxt",
            nxt_actionable=False,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    # Rules 5–14 are implemented in Task 4.
    return PendingReconciliationItem(
        order_id=order.order_id,
        symbol=order.symbol,
        market=order.market,
        side=order.side,
        classification="unknown",
        nxt_actionable=nxt_actionable,
        gap_pct=None,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        decision_support=decision_support,
    )
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: all 9 tests pass (2 from Task 2 + 7 from Task 3).

- [ ] **Step 5: Commit.**

```bash
git add app/services/pending_reconciliation_service.py tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
feat(rob-22): classify unknown venue, data mismatch, and KR non-NXT pendings

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 4 — Implement gap / near_fill / too_far / maintain (rules 5–11, 13)

**Files:**
- Modify: `app/services/pending_reconciliation_service.py`
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing tests.**

Append:
```python
def _ctx_with_quote(price: str, *, as_of=None, kr_eligible: bool | None = True) -> MarketContextInput:
    kr = (
        None
        if kr_eligible is None
        else KrUniverseContext(nxt_eligible=kr_eligible)
    )
    return MarketContextInput(
        quote=QuoteContext(price=Decimal(price), as_of=as_of),
        orderbook=None,
        support_resistance=None,
        kr_universe=kr,
    )


@pytest.mark.unit
def test_near_fill_buy() -> None:
    # ordered 70000, current 70200 → gap +0.2857%, |gap| <= 0.5
    item = reconcile_pending_order(_order(), _ctx_with_quote("70200"))
    assert item.classification == "near_fill"
    assert item.gap_pct is not None
    assert abs(item.gap_pct - Decimal("0.2857")) < Decimal("0.001")


@pytest.mark.unit
def test_too_far_buy_through_market() -> None:
    # buy at 70000 but market is 80000 → +14.28%, signed_distance_to_fill = -14.28
    item = reconcile_pending_order(_order(), _ctx_with_quote("80000"))
    assert item.classification == "too_far"
    assert "gap_against_fill_exceeds_too_far_pct" in item.reasons


@pytest.mark.unit
def test_too_far_sell_through_market() -> None:
    # sell at 70000 but market is 60000 → -14.28%, signed_distance_to_fill = -14.28
    item = reconcile_pending_order(
        _order(side="sell"), _ctx_with_quote("60000")
    )
    assert item.classification == "too_far"


@pytest.mark.unit
def test_maintain_default() -> None:
    # buy 70000, current 68000 → gap -2.857%, signed_distance_to_fill = +2.857%
    # |gap| > near_fill (0.5) and < chasing (3.0) → maintain
    item = reconcile_pending_order(_order(), _ctx_with_quote("68000"))
    assert item.classification == "maintain"


@pytest.mark.unit
def test_unknown_when_quote_missing() -> None:
    item = reconcile_pending_order(_order(), _empty_context(
        kr_universe=KrUniverseContext(nxt_eligible=True)
    ))
    assert item.classification == "unknown"
    assert "missing_quote" in item.warnings
    assert item.gap_pct is None


@pytest.mark.unit
def test_stale_quote_warning_still_classifies() -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    stale_at = now - timedelta(seconds=600)
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=stale_at),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(), ctx, now=now)
    assert "stale_quote" in item.warnings
    assert item.classification == "near_fill"


@pytest.mark.unit
def test_decision_support_includes_gap_and_signed_distance() -> None:
    item = reconcile_pending_order(_order(), _ctx_with_quote("68000"))
    ds = item.decision_support
    assert ds["current_price"] == Decimal("68000")
    assert ds["gap_pct"] is not None
    assert ds["signed_distance_to_fill"] is not None
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: 7 new failures.

- [ ] **Step 3: Implement rules 5–11 + 13 + decision-support population.**

In `app/services/pending_reconciliation_service.py` replace the post-`is_non_nxt` block with:
```python
    quote = context.quote
    if quote is None:
        warnings.append("missing_quote")
        return PendingReconciliationItem(
            order_id=order.order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            classification="unknown",
            nxt_actionable=nxt_actionable,
            gap_pct=None,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if quote.as_of is not None:
        reference = now or order.ordered_at
        if reference is not None:
            age = (reference - quote.as_of).total_seconds()
            if age > cfg.quote_stale_seconds:
                warnings.append("stale_quote")

    if context.orderbook is None:
        warnings.append("missing_orderbook")
    if context.support_resistance is None:
        warnings.append("missing_support_resistance")

    gap_pct = (
        (quote.price - order.ordered_price) / order.ordered_price * Decimal("100")
    )
    signed_distance_to_fill = -gap_pct if order.side == "buy" else gap_pct
    decision_support["current_price"] = quote.price
    decision_support["gap_pct"] = gap_pct
    decision_support["signed_distance_to_fill"] = signed_distance_to_fill

    sr = context.support_resistance
    if sr is not None:
        if sr.nearest_support is not None:
            decision_support["nearest_support_price"] = sr.nearest_support.price
            decision_support["nearest_support_distance_pct"] = (
                sr.nearest_support.distance_pct
            )
        if sr.nearest_resistance is not None:
            decision_support["nearest_resistance_price"] = sr.nearest_resistance.price
            decision_support["nearest_resistance_distance_pct"] = (
                sr.nearest_resistance.distance_pct
            )

    ob = context.orderbook
    if ob is not None and ob.best_bid is not None and ob.best_ask is not None:
        bid = ob.best_bid.price
        ask = ob.best_ask.price
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
            decision_support["bid_ask_spread_pct"] = spread_pct

    abs_gap = abs(gap_pct)
    classification: Classification

    if abs_gap <= cfg.near_fill_pct:
        classification = "near_fill"
        reasons.append("gap_within_near_fill_pct")
    elif signed_distance_to_fill < 0 and abs_gap >= cfg.too_far_pct:
        classification = "too_far"
        reasons.append("gap_against_fill_exceeds_too_far_pct")
    else:
        classification = "maintain"

    return PendingReconciliationItem(
        order_id=order.order_id,
        symbol=order.symbol,
        market=order.market,
        side=order.side,
        classification=classification,
        nxt_actionable=nxt_actionable,
        gap_pct=gap_pct,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        decision_support=decision_support,
    )
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/pending_reconciliation_service.py tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
feat(rob-22): classify near_fill / too_far / maintain with stale-quote warnings

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 5 — Implement chasing_risk (rule 12)

**Files:**
- Modify: `app/services/pending_reconciliation_service.py`
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing tests.**

Append:
```python
from app.services.pending_reconciliation_service import (
    SupportResistanceContext,
    SupportResistanceLevel,
)


@pytest.mark.unit
def test_chasing_risk_buy_into_resistance() -> None:
    # buy at 70000, current 68000 → gap -2.857% (signed_distance_to_fill +2.857)
    # → does NOT exceed chasing_pct (3.0). Use a wider gap to qualify.
    # buy at 70000, current 67000 → gap -4.285% (signed_distance_to_fill +4.285)
    sr = SupportResistanceContext(
        nearest_support=None,
        nearest_resistance=SupportResistanceLevel(
            price=Decimal("67500"), distance_pct=Decimal("0.5")
        ),
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("67000"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(), ctx)
    assert item.classification == "chasing_risk"
    assert "price_diverged_into_resistance" in item.reasons


@pytest.mark.unit
def test_chasing_risk_sell_into_support() -> None:
    sr = SupportResistanceContext(
        nearest_support=SupportResistanceLevel(
            price=Decimal("72500"), distance_pct=Decimal("0.5")
        ),
        nearest_resistance=None,
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("73000"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(side="sell"), ctx)
    assert item.classification == "chasing_risk"
    assert "price_diverged_into_support" in item.reasons


@pytest.mark.unit
def test_chasing_risk_skipped_when_sr_missing() -> None:
    # Same gap as above but no SR → falls back to maintain (gap is 4.28%, not too_far)
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("67000"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = reconcile_pending_order(_order(), ctx)
    assert item.classification == "maintain"
    assert "missing_support_resistance" in item.warnings
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: 3 new failures.

- [ ] **Step 3: Insert chasing_risk between `too_far` and `maintain` in the classifier.**

Replace the `else: classification = "maintain"` branch with:
```python
    elif (
        signed_distance_to_fill > cfg.chasing_pct
        and context.support_resistance is not None
        and (
            (
                order.side == "buy"
                and context.support_resistance.nearest_resistance is not None
                and context.support_resistance.nearest_resistance.distance_pct
                <= cfg.chasing_resistance_pct
            )
            or (
                order.side == "sell"
                and context.support_resistance.nearest_support is not None
                and context.support_resistance.nearest_support.distance_pct
                <= cfg.chasing_support_pct
            )
        )
    ):
        classification = "chasing_risk"
        reasons.append(
            "price_diverged_into_resistance"
            if order.side == "buy"
            else "price_diverged_into_support"
        )
    else:
        classification = "maintain"
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/pending_reconciliation_service.py tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
feat(rob-22): classify chasing_risk against nearest support/resistance

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 6 — Implement `reconcile_pending_orders` and configurability tests

**Files:**
- Modify: `app/services/pending_reconciliation_service.py`
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing tests.**

Append:
```python
from app.services.pending_reconciliation_service import (
    ReconciliationConfig,
    reconcile_pending_orders,
)


@pytest.mark.unit
def test_reconcile_pending_orders_pairs_orders_and_contexts() -> None:
    o1 = _order(order_id="A", symbol="005930")
    o2 = _order(order_id="B", symbol="034220", currency="KRW")
    contexts = {
        "A": _ctx_with_quote("70200"),  # near_fill
        "B": MarketContextInput(
            quote=None,
            orderbook=None,
            support_resistance=None,
            kr_universe=KrUniverseContext(nxt_eligible=False),
        ),
    }
    items = reconcile_pending_orders([o1, o2], contexts)
    assert {item.order_id for item in items} == {"A", "B"}
    by_id = {item.order_id: item for item in items}
    assert by_id["A"].classification == "near_fill"
    assert by_id["B"].classification == "kr_pending_non_nxt"


@pytest.mark.unit
def test_reconcile_pending_orders_treats_missing_context_as_empty() -> None:
    o = _order(order_id="X")
    items = reconcile_pending_orders([o], {})
    assert len(items) == 1
    assert "missing_quote" in items[0].warnings


@pytest.mark.unit
def test_config_overrides_thresholds() -> None:
    # Default near_fill_pct is 0.5; with 5.0 override, 2.857% gap is near_fill.
    item = reconcile_pending_order(
        _order(),
        _ctx_with_quote("68000"),
        config=ReconciliationConfig(near_fill_pct=Decimal("5.0")),
    )
    assert item.classification == "near_fill"
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: 3 new failures (NotImplementedError for `reconcile_pending_orders`).

- [ ] **Step 3: Implement `reconcile_pending_orders`.**

Replace the body in `app/services/pending_reconciliation_service.py`:
```python
def reconcile_pending_orders(
    orders: Sequence[PendingOrderInput],
    contexts_by_order_id: dict[str, MarketContextInput],
    *,
    config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> list[PendingReconciliationItem]:
    empty = MarketContextInput(
        quote=None,
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    return [
        reconcile_pending_order(
            order,
            contexts_by_order_id.get(order.order_id, empty),
            config=config,
            now=now,
        )
        for order in orders
    ]
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/pending_reconciliation_service.py tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
feat(rob-22): batch reconciliation entry point with per-order context lookup

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 7 — Reusability test mimicking both call sites

**Files:**
- Modify: `tests/services/test_pending_reconciliation_service.py`

- [ ] **Step 1: Write the failing test.**

Append:
```python
@pytest.mark.unit
def test_two_callers_share_one_pure_service() -> None:
    """Demonstrate Research Run live refresh and Decision Session proposal
    generation can both call the service with their own context shapes
    without importing each other.
    """

    # 1. "Research Run live refresh" caller: builds context inline from
    # already-fetched quote + KR universe row.
    research_order = _order(order_id="research-1", symbol="005930")
    research_context = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    research_item = reconcile_pending_order(research_order, research_context)
    assert research_item.classification == "near_fill"

    # 2. "Decision Session proposal generation" caller: builds context from
    # SR + orderbook to drive proposal warnings.
    proposal_order = _order(
        order_id="proposal-1", symbol="034220", currency="KRW"
    )
    proposal_context = MarketContextInput(
        quote=QuoteContext(price=Decimal("9800"), as_of=None),
        orderbook=None,
        support_resistance=SupportResistanceContext(
            nearest_support=None,
            nearest_resistance=SupportResistanceLevel(
                price=Decimal("9850"),
                distance_pct=Decimal("0.5"),
            ),
        ),
        kr_universe=KrUniverseContext(nxt_eligible=False),
    )
    proposal_item = reconcile_pending_order(
        PendingOrderInput(
            order_id="proposal-1",
            symbol="034220",
            market="kr",
            side="buy",
            ordered_price=Decimal("9500"),
            ordered_qty=Decimal("100"),
            remaining_qty=Decimal("100"),
            currency="KRW",
            ordered_at=None,
        ),
        proposal_context,
    )
    # Non-NXT KR pending always wins over chasing_risk (rule 3 fires before quote rules).
    assert proposal_item.classification == "kr_pending_non_nxt"
    assert proposal_item.nxt_actionable is False
    assert "non_nxt_venue" in proposal_item.warnings
```

- [ ] **Step 2: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
Expected: all tests pass (the test is constructive — it should already pass against the implementation from Tasks 3–6).

- [ ] **Step 3: Commit.**

```bash
git add tests/services/test_pending_reconciliation_service.py
git commit -m "$(cat <<'EOF'
test(rob-22): show service is reusable from both Research Run and Decision Session callers

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 8 — Safety: forbid broker / order / DB / Redis transitive imports

**Files:**
- Create: `tests/services/test_pending_reconciliation_service_safety.py`

- [ ] **Step 1: Write the failing test.**

Write the file:
```python
"""Safety: pending reconciliation service must stay pure.

Modeled on tests/services/test_operator_decision_session_safety.py — runs
the import in a clean subprocess and inspects sys.modules to verify the
service does not transitively pull in broker, order-execution, watch-alert,
paper-order, fill-notification, KIS-websocket, DB, or Redis modules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.orders",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.screener_service",
    "app.services.n8n_pending_orders_service",
    "app.services.n8n_pending_review_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
    "app.core.db",
    "redis",
    "httpx",
    "sqlalchemy",
]


def _loaded_modules_after_import(module_name: str) -> set[str]:
    project_root = Path(__file__).resolve().parents[2]
    script = f"""
import importlib
import json
import sys

importlib.import_module({module_name!r})
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(json.loads(result.stdout))


@pytest.mark.unit
def test_pending_reconciliation_service_is_pure() -> None:
    loaded = _loaded_modules_after_import(
        "app.services.pending_reconciliation_service"
    )
    violations = sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")
```

- [ ] **Step 2: Run tests; verify they pass against the existing module.**

Run: `uv run pytest tests/services/test_pending_reconciliation_service_safety.py -v`
Expected: pass. If it fails, the implementation accidentally imported a forbidden module — fix the import in `app/services/pending_reconciliation_service.py` (it should use only `dataclasses`, `datetime`, `decimal`, and `typing`).

- [ ] **Step 3: Commit.**

```bash
git add tests/services/test_pending_reconciliation_service_safety.py
git commit -m "$(cat <<'EOF'
test(rob-22): enforce pending reconciliation service has no broker / DB imports

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 9 — Lint, typecheck, full test sweep

**Files:** none

- [ ] **Step 1: Run lint.**

Run: `make lint`
Expected: clean. If Ruff complains about long files / cognitive complexity in `reconcile_pending_order`, suppress with the documented `# noqa: C901` already added; fix any other issues at root.

- [ ] **Step 2: Run typecheck.**

Run: `make typecheck`
Expected: no new errors in `app/services/pending_reconciliation_service.py` or its test files.

- [ ] **Step 3: Run the unit-test scope.**

Run: `make test-unit`
Expected: all green; no regressions introduced.

- [ ] **Step 4: Commit any lint/format-only changes (if any).**

```bash
git status
# If only auto-formatter changes appear:
git add -p
git commit -m "$(cat <<'EOF'
chore(rob-22): apply ruff formatting

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 10 — Open the PR

**Files:** none

- [ ] **Step 1: Push the branch.**

```bash
git push -u origin feature/ROB-22-pending-reconciliation-service
```

- [ ] **Step 2: Open PR against `main`.**

```bash
gh pr create --base main --title "ROB-22: Add pending reconciliation service (pure, read-only)" --body "$(cat <<'EOF'
## Summary
- Add `app/services/pending_reconciliation_service.py`: pure dataclasses + classifier that converts broker pending orders + market context into decision-support items.
- Classifications: `maintain`, `near_fill`, `too_far`, `chasing_risk`, `data_mismatch`, `kr_pending_non_nxt`, `unknown_venue`, `unknown` (fallback when quote is missing).
- Warnings: `missing_quote`, `stale_quote`, `missing_orderbook`, `missing_support_resistance`, `missing_kr_universe`, `non_nxt_venue`, `unknown_venue`, `unknown_side`.
- KR/NXT awareness via the caller-supplied `KrUniverseContext` (caller resolves it via `KrSymbolUniverseService.is_nxt_eligible`); non-NXT KR pendings (e.g. 034220) classify as `kr_pending_non_nxt` with `nxt_actionable=false`.

## Trading-safety invariants
- Pure function module; no `place_order` / `modify_order` / `cancel_order` / watch-alert / paper / dry-run / fill-notification / broker / DB / Redis imports.
- Subprocess `sys.modules` test (`tests/services/test_pending_reconciliation_service_safety.py`) enforces the import isolation, modeled on `tests/services/test_operator_decision_session_safety.py`.
- No new mutation paths or order side effects. Decision Session creation is unaffected.

## Out of scope
- ROB-20 live refresh wiring / UI rendering: caller-side concerns.
- API endpoint, Prefect flow, dashboard.
- Persisting reconciliation results to the DB.

## Test plan
- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `uv run pytest tests/services/test_pending_reconciliation_service.py -v`
- [ ] `uv run pytest tests/services/test_pending_reconciliation_service_safety.py -v`
- [ ] `make test-unit`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Capture the PR URL in the AoE session log.**

---

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| A future refactor accidentally imports a broker module into `pending_reconciliation_service.py`. | `tests/services/test_pending_reconciliation_service_safety.py` runs in a subprocess and fails the build on any forbidden prefix. |
| Decimal/float coercion bugs distort thresholds (e.g., `0.1 + 0.2`). | The DTOs use `Decimal` throughout; tests use `Decimal` literals; thresholds are `Decimal` constants in `ReconciliationConfig`. |
| Missing context types differ across callers (e.g., one passes a partial dict, another passes None). | The service accepts `MarketContextInput` only; each caller is responsible for shaping the DTO. `reconcile_pending_orders` substitutes a fully-empty `MarketContextInput` when the per-order entry is missing, so both callers get consistent warnings. |
| Stale quotes are accepted silently. | `quote.as_of` is mandatory in the DTO (callers can pass `None` to opt out); when present, age is checked against `config.quote_stale_seconds` and a `stale_quote` warning is emitted. |
| Misclassifying KR non-NXT as actionable. | Rule 3 fires before any quote-dependent rule, so `kr_pending_non_nxt` overrides `near_fill` / `too_far` / `chasing_risk`. Tested explicitly in Tasks 3 and 7. |
| ROB-22 work creeps into ROB-20 wiring. | Plan calls out: do not add API endpoints, UI templates, Prefect flows, or persistence. Reviewer should reject any new caller in this PR; consumers come in a follow-up. |

## PR scope (reviewer checklist)

- Adds: `app/services/pending_reconciliation_service.py`, `tests/services/test_pending_reconciliation_service.py`, `tests/services/test_pending_reconciliation_service_safety.py`, `docs/plans/ROB-22-pending-reconciliation-service-plan.md`.
- Does **not** modify: `app/mcp_server/tooling/orders_*`, `app/services/trading_decision_service.py`, `app/services/operator_decision_session_service.py`, `app/services/tradingagents_research_service.py`, `app/services/n8n_pending_*`, models, schemas, alembic, routers, Prefect flows, UI templates.
- No new env vars, no new dependencies in `pyproject.toml` / `uv.lock`.
- No DB migrations.
- No broker mutation, no watch alert registration, no paper/dry-run/live order placement.
