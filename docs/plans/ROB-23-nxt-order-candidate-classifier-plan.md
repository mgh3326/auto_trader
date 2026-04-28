# ROB-23 — NXT-specific Order / Candidate / Holding Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear issue:** ROB-23 — [Classifier] Add NXT-specific order and candidate classifier

**Absorbs:** ROB-29 (non-NXT KR pending exclusion + fail-closed handling for missing KR universe rows). ROB-29 does **not** start a separate implementation worktree — its classifier-level behavior is delivered as part of this ROB-23 PR. Anything outside the pure classifier (live-refresh wiring, Decision Session integration, UI rendering, Prefect orchestration) remains a wiring follow-up tracked under ROB-25 or later.

**Goal:** Add a pure, read-only KR NXT-specific classifier that interprets pending orders, candidates, and holdings together with quote / orderbook / support-resistance / NXT-eligibility context, producing decision-support classifications and an operator-facing summary line for after-hours (NXT) decision support. Build on top of `app/services/pending_reconciliation_service.py` (ROB-22) — do **not** duplicate its venue / data-mismatch / non-NXT / chasing logic.

**ROB-29 absorption note (must hold across the implementation):**
- KIS source semantics. `get_order_history(status="pending", market="kr")` reads KIS `/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl` (TR_ID `TTTC8036R`, 국내주식 정정취소가능주문조회). It is **not** an NXT-only pending list; the broker may return KR pending orders it considers still modifiable/cancellable even after the regular session closes, regardless of NXT eligibility.
- Per-order NXT eligibility is mandatory in the classifier context. Every KR pending order / candidate / holding fed into this classifier must be paired with a `KrUniverseContext` whose `nxt_eligible` field comes from `kr_symbol_universe.nxt_eligible` (resolved by callers via `KrSymbolUniverseService.is_nxt_eligible(symbol)`).
- Non-NXT KR pendings (e.g. 034220 LG디스플레이) must classify as `non_nxt_pending_ignore_for_nxt` with `nxt_actionable=False` and must be excluded from any downstream NXT modify / cancel / execution candidate output. The 034220 fixture is required in tests.
- Fail-closed for missing KR universe rows. When `order.market == "kr"` and `KrUniverseContext` is missing (ROB-22 emits warning `missing_kr_universe`), the classifier must **never** return any `*_actionable` / `*_at_support` / `*_near_resistance` label. It returns `data_mismatch_requires_review` with `nxt_actionable=False`, propagates the `missing_kr_universe` warning, and adds reason `missing_kr_universe_fail_closed`. Default-to-actionable is forbidden.

**Architecture:**
- One new module `app/services/nxt_classifier_service.py` containing only pure dataclasses and pure functions. No broker / DB / HTTP / Redis / TaskIQ / KIS-websocket imports. No mutation, no order placement, no watch-alert registration.
- Pending-order classification delegates to `reconcile_pending_order(...)` from `app.services.pending_reconciliation_service`, then maps the result into NXT-specific labels using the order price's proximity to nearest support (for buys) or nearest resistance (for sells).
- Candidate classification reuses the same pipeline by adapting an `NxtCandidateInput` into a `PendingOrderInput`-compatible shape (`remaining_qty == proposed_qty or proposed_qty or 1`) and feeding it through the reconciliation service. The `kind` field on the output discriminates pending-order vs candidate.
- Holding classification is a separate, simpler rule set (no fill-vs-market gap logic): NXT-eligible held position → `holding_watch_only`; non-NXT held position → `non_nxt_pending_ignore_for_nxt`; data mismatch (currency, non-positive qty) → `data_mismatch_requires_review`.
- Spread / liquidity warnings (`wide_spread`, `thin_liquidity`) are emitted as warnings only — they do not change the classification.
- Output includes a concise Korean operator-facing `summary` field suitable for proposal-card rendering. Templates are deterministic per classification; no LLM calls.

**Tech stack:** Python 3.13, `dataclasses`, `typing`, `decimal.Decimal`, `pytest`. No new third-party dependencies.

**ROB-20 / ROB-22 / ROB-25 / ROB-29 boundaries:**
- ROB-20 (live refresh wiring + UI rendering, API endpoints, Prefect flows) is **out of scope**. If a behavior here genuinely requires ROB-20-side wiring, stop and report it as a blocker.
- ROB-22 already merged (commit `23ac923c`) — its module is the dependency. Do not re-implement venue / data-mismatch / non-NXT / chasing checks here; consume the reconciliation result.
- ROB-25 (or later wiring follow-up) owns connecting this classifier to live-refresh callers, Decision Session proposal generation, and any UI/Prefect rendering. ROB-23 stops at the pure classifier + tests.
- ROB-29 is absorbed into this PR at the classifier layer only. No separate ROB-29 worktree is opened. ROB-29 acceptance is satisfied by Tasks 3, 5, 6, and the fail-closed cases in Task 7's data-driven matrix.

**Trading-safety guardrails (non-negotiable):**
- Read-only / decision-support only. No `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, paper / dry-run / live order placement, watch registration, or order-intent creation introduced.
- The new module must not transitively import broker / order-execution / watch-alert / paper-order / fill-notification / KIS-websocket / DB / Redis modules. Enforced by a subprocess `sys.modules` test (Task 9) modeled on `tests/services/test_pending_reconciliation_service_safety.py`.
- TradingAgents output, if eventually wired in by a follow-up, must remain `advisory_only=true / execution_allowed=false`. ROB-23 itself does not import or invoke TradingAgents.
- Decision Session creation = ledger persistence only; ROB-23 does not create sessions or proposals.
- No secrets, API keys, tokens, or account numbers are read or printed by this code.

---

## File Structure

| Path | Status | Responsibility |
|------|--------|----------------|
| `app/services/nxt_classifier_service.py` | create | Pure dataclasses + NXT classifier functions for pending orders, candidates, holdings. Delegates to `pending_reconciliation_service`. |
| `tests/services/test_nxt_classifier_service.py` | create | Unit tests covering eligible / non-eligible × buy / sell × near / far × missing-data cases, plus holding watch-only and spread warnings. |
| `tests/services/test_nxt_classifier_service_safety.py` | create | Subprocess `sys.modules` test asserting the module does not transitively import broker / order-execution / watch-alert / paper / fill-notification / DB / Redis modules. |
| `docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md` | create (this file) | Implementation plan. |

No changes to:
- `app/services/pending_reconciliation_service.py` (consume only).
- `app/services/trading_decision_service.py`, `app/services/operator_decision_session_service.py`, `app/services/tradingagents_research_service.py` (consumers come in a follow-up issue/PR).
- `app/services/kr_symbol_universe_service.py` (callers resolve NXT eligibility themselves and pass the boolean as `KrUniverseContext.nxt_eligible`).
- `app/services/kis_holdings_service.py`, `app/services/n8n_pending_*` (caller-side concerns).
- `app/mcp_server/*`, `app/models/*`, `app/schemas/*`, `alembic/*`, routers, Prefect flows, UI templates.

---

## Domain Reference (read once before coding)

- **KIS KR pending source (ROB-29 clarification).** `get_order_history(status="pending", market="kr")` calls KIS `/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl` with TR_ID `TTTC8036R` (국내주식 정정취소가능주문조회 — *Korean domestic modify/cancel-eligible order inquiry*). This is **not** an NXT-only list: the broker may include any KR pending order it still considers eligible for amendment/cancellation, including standard KRX-routed orders, even after the regular session closes. The classifier must not assume every KR pending row is NXT-routable; it must consult `kr_symbol_universe.nxt_eligible` per row.
- **NXT eligibility source.** `kr_symbol_universe.nxt_eligible` (`app/models/kr_symbol_universe.py:23`). Helper: `app.services.kr_symbol_universe_service.is_nxt_eligible(symbol)` (`app/services/kr_symbol_universe_service.py:363`). Default to `False` when missing or inactive. Callers resolve the bool themselves and pass it via `KrUniverseContext` from the reconciliation module. The NXT classifier is fail-closed: a missing `KrUniverseContext` for a KR row is *never* treated as eligible.
- **Important non-NXT example.** `034220` (LG디스플레이) appears in `inquire_korea_orders` output but has `nxt_eligible=False`. It must classify as `non_nxt_pending_ignore_for_nxt` regardless of side / gap / S-R proximity, and must be excluded from NXT modify / cancel / execution candidate consumers downstream. This fixture is mandatory in the test matrix.
- **Reconciliation classifications (ROB-22)** — see `Classification = Literal[...]` in `app/services/pending_reconciliation_service.py:17-26`:
  - `maintain`, `near_fill`, `too_far`, `chasing_risk`, `data_mismatch`, `kr_pending_non_nxt`, `unknown_venue`, `unknown`.
- **Reconciliation context shapes** (reused verbatim by ROB-23 — do **not** redefine):
  - `PendingOrderInput`, `QuoteContext`, `OrderbookLevelContext`, `OrderbookContext`, `SupportResistanceLevel`, `SupportResistanceContext`, `KrUniverseContext`, `MarketContextInput`, `ReconciliationConfig`, `PendingReconciliationItem`.
- **`PendingReconciliationItem.decision_support`** keys we will rely on:
  - `current_price`, `gap_pct`, `signed_distance_to_fill`, `nearest_support_price`, `nearest_support_distance_pct`, `nearest_resistance_price`, `nearest_resistance_distance_pct`, `bid_ask_spread_pct`.
  - `nearest_support_distance_pct` / `nearest_resistance_distance_pct` are caller-supplied measurements relative to **current price** (per ROB-22 plan section "Domain Reference"). For determining whether the **order price** sits near a support / resistance level, we recompute it ourselves from `nearest_support_price` / `nearest_resistance_price` and `order.ordered_price`.

---

## Public API of the Service

```python
# app/services/nxt_classifier_service.py
from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.services.pending_reconciliation_service import (
    MarketContextInput,
    PendingOrderInput,
    PendingReconciliationItem,
    ReconciliationConfig,
    reconcile_pending_order,
)

NxtClassification = Literal[
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
]
NxtKind = Literal["pending_order", "candidate", "holding"]


@dataclass(frozen=True, slots=True)
class NxtCandidateInput:
    candidate_id: str
    symbol: str
    side: str          # "buy" / "sell"; service validates
    proposed_price: Decimal
    proposed_qty: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtHoldingInput:
    holding_id: str
    symbol: str
    quantity: Decimal
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtClassifierConfig:
    near_support_pct: Decimal = Decimal("1.0")
    near_resistance_pct: Decimal = Decimal("1.0")
    wide_spread_pct: Decimal = Decimal("1.0")
    # If both bid+ask total qty are below this, emit "thin_liquidity".
    thin_liquidity_total_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NxtClassifierItem:
    item_id: str
    symbol: str
    kind: NxtKind
    side: str | None
    classification: NxtClassification
    nxt_actionable: bool
    summary: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


def classify_nxt_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem: ...


def classify_nxt_candidate(
    candidate: NxtCandidateInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem: ...


def classify_nxt_holding(
    holding: NxtHoldingInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
) -> NxtClassifierItem: ...
```

**Mapping rules — pending orders & candidates (executed in this order; first match wins):**

Inputs: a `PendingReconciliationItem` from ROB-22, the order/candidate's `market`, `side`, `ordered_price`, and `nxt_cfg = config or NxtClassifierConfig()`.

1. If `recon.classification == "unknown_venue"` → `data_mismatch_requires_review`.
2. If `recon.classification == "data_mismatch"` → `data_mismatch_requires_review`.
3. If `recon.classification == "kr_pending_non_nxt"` → `non_nxt_pending_ignore_for_nxt`.
4. **Fail-closed for missing KR universe (ROB-29).** If `market == "kr"` and `"missing_kr_universe" in recon.warnings` (ROB-22 emits this when `KrUniverseContext` is `None`) → `data_mismatch_requires_review`, append reason `missing_kr_universe_fail_closed`, `nxt_actionable=False`. This rule fires before any quote-dependent rule and overrides any `*_actionable` / `*_at_support` / `*_near_resistance` outcome that the recon classification would otherwise produce. Default-to-actionable is forbidden.
5. If `recon.classification == "unknown"` (i.e., quote missing) → `unknown`.
6. If `recon.classification == "too_far"`:
   - `side == "buy"` → `buy_pending_too_far`.
   - `side == "sell"` → `sell_pending_too_optimistic`.
7. If `recon.classification == "chasing_risk"`:
   - `side == "buy"` → `buy_pending_too_far` (price has run away upward into resistance; the buy is stranded too low).
   - `side == "sell"` → `sell_pending_too_optimistic` (price has run away downward into support; the sell is stranded too high).
8. Compute `order_to_support_pct` and `order_to_resistance_pct` from the order's `ordered_price` and the reconciliation `decision_support`'s `nearest_support_price` / `nearest_resistance_price`:
   - `order_to_support_pct = abs(order.ordered_price - support_price) / order.ordered_price * 100` (skip if `support_price is None`).
   - `order_to_resistance_pct = abs(order.ordered_price - resistance_price) / order.ordered_price * 100` (skip if `resistance_price is None`).
9. For `recon.classification in {"near_fill", "maintain"}`:
   - `side == "buy"`: if `order_to_support_pct is not None and order_to_support_pct <= nxt_cfg.near_support_pct` → `buy_pending_at_support`; else → `buy_pending_actionable`.
   - `side == "sell"`: if `order_to_resistance_pct is not None and order_to_resistance_pct <= nxt_cfg.near_resistance_pct` → `sell_pending_near_resistance`; else → `sell_pending_actionable`.

The reconciliation item's `warnings`, `gap_pct`, and `decision_support` are propagated verbatim to the NXT item. Reasons are propagated and extended with NXT-specific reason codes (`order_within_near_support_pct`, `order_within_near_resistance_pct`).

`nxt_actionable` is `True` iff the final classification is one of `buy_pending_at_support`, `buy_pending_actionable`, `sell_pending_near_resistance`, `sell_pending_actionable`. It is `False` for `non_nxt_pending_ignore_for_nxt` / `data_mismatch_requires_review` / `holding_watch_only` / `unknown`. It is `False` for `*_too_far` / `*_too_optimistic` (these are flagged for review, not as actionable proposals).

**Mapping rules — holdings:**

1. If `holding.quantity is None or holding.quantity <= 0` → `data_mismatch_requires_review`, reason `non_positive_quantity`.
2. If `holding.currency` is set and not `"KRW"` → `data_mismatch_requires_review`, reason `currency_mismatch`.
3. If `context.kr_universe is None` → `holding_watch_only` with warning `missing_kr_universe`, `nxt_actionable=False` (we cannot confirm NXT routing).
4. If `context.kr_universe.nxt_eligible is False` → `non_nxt_pending_ignore_for_nxt`, `nxt_actionable=False`, warning `non_nxt_venue`.
5. Otherwise → `holding_watch_only`, `nxt_actionable=False` (watch only — not an actionable proposal).

`gap_pct` is always `None` for holdings (there is no order price to compare). `decision_support` is populated from the context (current price, S/R, spread) when available, but no signed-distance-to-fill is computed.

**Spread / liquidity warnings (applied uniformly to all kinds when an `OrderbookContext` is present):**

- If `decision_support["bid_ask_spread_pct"]` is set and `> nxt_cfg.wide_spread_pct` → append warning `wide_spread`.
- If `nxt_cfg.thin_liquidity_total_qty` is not `None` and `(orderbook.total_bid_qty or 0) + (orderbook.total_ask_qty or 0) < threshold` → append warning `thin_liquidity`.

**Operator-facing summary (deterministic Korean string):**

| classification | summary template |
|---|---|
| `buy_pending_at_support` | `"NXT 매수 대기 — 지지선 근접 (지지선 {support_price})"` |
| `buy_pending_actionable` | `"NXT 매수 대기 — 적정 (지속 모니터링)"` |
| `buy_pending_too_far` | `"NXT 매수 대기 — 시장가 대비 이격 큼 (재검토 필요)"` |
| `sell_pending_near_resistance` | `"NXT 매도 대기 — 저항선 근접 (저항선 {resistance_price})"` |
| `sell_pending_actionable` | `"NXT 매도 대기 — 적정 (지속 모니터링)"` |
| `sell_pending_too_optimistic` | `"NXT 매도 대기 — 시장가 대비 너무 낙관적 (재검토 필요)"` |
| `non_nxt_pending_ignore_for_nxt` | `"KR 일반종목 — NXT 대상 아님 (NXT 의사결정에서 제외)"` |
| `holding_watch_only` | `"NXT 보유 — 신규 액션 없음, 모니터링 대상"` |
| `data_mismatch_requires_review` | `"주문/포지션 데이터 불일치 — 운영자 검토 필요"` |
| `unknown` | `"NXT 분류 불가 — 시세 정보 부족"` |

`{support_price}` / `{resistance_price}` are formatted via `format(Decimal, ',f')` with trailing zeros stripped via `.normalize()` then `str()`. If the level price is missing for the "_at_support" / "_near_resistance" templates (which cannot happen given the classification gates), fall back to the plain `_actionable` template wording.

---

## Self-Review

### Spec coverage

- **AC: Non-NXT symbols are excluded from NXT-actionable states.** Pending/candidate rule 3 returns `non_nxt_pending_ignore_for_nxt` with `nxt_actionable=False`. Holding rule 4 returns the same. Tested in Tasks 3, 5, and 6 (the 034220 LG디스플레이 fixture appears in `test_pending_non_nxt_kr_034220_maps_to_non_nxt_pending_ignore` and `test_candidate_non_nxt_034220_excluded`).
- **AC: Pending buy/sell orders are compared against support/resistance and current NXT context.** Pending/candidate rules 8–9 compute order-price-to-S/R proximity. Tested in Task 4 (`test_buy_pending_at_support`, `test_sell_pending_near_resistance`, `test_buy_pending_actionable_when_support_far`, etc.).
- **AC: Result includes concise operator-facing summary for proposal cards.** `NxtClassifierItem.summary` field; deterministic templates per classification. Tested in Task 8.
- **AC: Tests cover eligible / non-eligible, buy / sell, near / far, and missing-data cases.** Task 4 covers eligible buy/sell at-support / near-resistance / too-far / actionable. Task 5 covers candidate parity. Task 6 covers holding eligible/non-eligible/data-mismatch. Task 7 covers missing-data fall-throughs. Task 8 covers spread and Korean summary.
- **AC: Spread/liquidity warnings when orderbook data is available.** Task 8 (`test_wide_spread_warning`, `test_thin_liquidity_warning_when_threshold_set`).
- **AC: Builds on the pending reconciliation service.** Tasks 3–5 delegate to `reconcile_pending_order`; ROB-23 does not re-implement venue / data-mismatch / non-NXT / chasing logic.
- **ROB-29 absorbed AC: KIS pending source documented + per-row NXT eligibility enforced.** Domain Reference paragraph names KIS `/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl` (TR_ID `TTTC8036R`, 국내주식 정정취소가능주문조회) and explicitly states this is not an NXT-only list. The mapper consults `kr_universe.nxt_eligible` per row via the reconciliation service.
- **ROB-29 absorbed AC: Non-NXT KR pending excluded with `nxt_actionable=false`.** Mapper rule 3 returns `non_nxt_pending_ignore_for_nxt`. `_is_nxt_actionable(...)` excludes that label. Tested via 034220 fixtures in pending and candidate kinds.
- **ROB-29 absorbed AC: Missing KR universe row is fail-closed.** Mapper rule 4 (`market == "kr"` + `missing_kr_universe` warning) overrides any actionable / at-support / near-resistance outcome to `data_mismatch_requires_review` with `nxt_actionable=False` and reason `missing_kr_universe_fail_closed`. Tested by `test_pending_kr_missing_universe_fails_closed_to_data_mismatch`, `test_pending_kr_missing_universe_overrides_at_support_attempt`, and `test_candidate_kr_missing_universe_fails_closed`.

### Placeholder scan

No "TBD" / "implement later" / "similar to Task N" present. Each task carries actual code/tests it expects.

### Type consistency

`NxtClassification`, `NxtKind`, `NxtCandidateInput`, `NxtHoldingInput`, `NxtClassifierConfig`, `NxtClassifierItem`, `MarketContextInput`, `PendingOrderInput` (from ROB-22), `classify_nxt_pending_order`, `classify_nxt_candidate`, `classify_nxt_holding` are referenced by the same names across all tasks. The internal helper `_map_recon_to_nxt(recon, *, market, side, order_price, nxt_cfg) -> tuple[NxtClassification, list[str]]` is defined once in Task 3 and reused by Task 5 (candidates pass `market="kr"`). The `market` keyword argument is required by the ROB-29 fail-closed rule (mapper rule 4); both call sites pass it.

---

## Tasks

### Task 1 — Plan only (this commit)

**Files:**
- Create: `docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md` (this file)

- [ ] **Step 1: Verify the plan file is on disk.**

Run:
```bash
test -f docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md && \
  git status docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md
```
Expected: file exists; either staged or already committed on the feature branch.

- [ ] **Step 2: Commit the plan.**

```bash
git add docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md
git commit -m "$(cat <<'EOF'
docs(rob-23): plan NXT-specific order/candidate/holding classifier

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 2 — Define the dataclass surface (no logic)

**Files:**
- Create: `app/services/nxt_classifier_service.py`
- Create: `tests/services/test_nxt_classifier_service.py`

- [ ] **Step 1: Write the failing test for module import + dataclass shape.**

Write `tests/services/test_nxt_classifier_service.py`:
```python
"""Unit tests for app.services.nxt_classifier_service.

These tests cover only the pure NXT classifier + summary logic. They do
not import any broker, DB, Redis, or HTTP module; the service module
under test must not transitively import them either (see
test_nxt_classifier_service_safety.py).
"""

from __future__ import annotations

from dataclasses import fields

import pytest


@pytest.mark.unit
def test_module_exposes_public_api() -> None:
    from app.services import nxt_classifier_service as svc

    assert hasattr(svc, "NxtCandidateInput")
    assert hasattr(svc, "NxtHoldingInput")
    assert hasattr(svc, "NxtClassifierConfig")
    assert hasattr(svc, "NxtClassifierItem")
    assert callable(svc.classify_nxt_pending_order)
    assert callable(svc.classify_nxt_candidate)
    assert callable(svc.classify_nxt_holding)


@pytest.mark.unit
def test_item_has_required_fields() -> None:
    from app.services.nxt_classifier_service import NxtClassifierItem

    expected = {
        "item_id",
        "symbol",
        "kind",
        "side",
        "classification",
        "nxt_actionable",
        "summary",
        "reasons",
        "warnings",
        "decision_support",
    }
    assert {f.name for f in fields(NxtClassifierItem)} == expected
```

- [ ] **Step 2: Run tests to verify they fail.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.nxt_classifier_service'`.

- [ ] **Step 3: Create the dataclass-only module.**

Write `app/services/nxt_classifier_service.py`:
```python
"""Pure NXT-specific classifier for KR pending orders, candidates, holdings.

Read-only / decision-support only. This module must not import broker,
order-execution, watch-alert, paper-order, fill-notification, KIS-websocket,
DB, or Redis modules. Callers collect their own context (orders, candidates,
holdings, quotes, orderbook, support/resistance, KR NXT eligibility) and
pass it as plain DTOs.

ROB-23 builds on `app.services.pending_reconciliation_service` (ROB-22).
Pending-order and candidate classification delegates to
`reconcile_pending_order(...)` and re-labels the result for NXT semantics.
Holding classification is independent (no fill-vs-market gap).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.services.pending_reconciliation_service import (
    MarketContextInput,
    PendingOrderInput,
    PendingReconciliationItem,
    ReconciliationConfig,
    reconcile_pending_order,
)

NxtClassification = Literal[
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
]
NxtKind = Literal["pending_order", "candidate", "holding"]


@dataclass(frozen=True, slots=True)
class NxtCandidateInput:
    candidate_id: str
    symbol: str
    side: str
    proposed_price: Decimal
    proposed_qty: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtHoldingInput:
    holding_id: str
    symbol: str
    quantity: Decimal
    currency: str | None


@dataclass(frozen=True, slots=True)
class NxtClassifierConfig:
    near_support_pct: Decimal = Decimal("1.0")
    near_resistance_pct: Decimal = Decimal("1.0")
    wide_spread_pct: Decimal = Decimal("1.0")
    thin_liquidity_total_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NxtClassifierItem:
    item_id: str
    symbol: str
    kind: NxtKind
    side: str | None
    classification: NxtClassification
    nxt_actionable: bool
    summary: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decision_support: dict[str, Decimal | str | None]


def classify_nxt_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    raise NotImplementedError


def classify_nxt_candidate(
    candidate: NxtCandidateInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    raise NotImplementedError


def classify_nxt_holding(
    holding: NxtHoldingInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
) -> NxtClassifierItem:
    raise NotImplementedError


__all__ = [
    "NxtClassification",
    "NxtKind",
    "NxtCandidateInput",
    "NxtHoldingInput",
    "NxtClassifierConfig",
    "NxtClassifierItem",
    "classify_nxt_pending_order",
    "classify_nxt_candidate",
    "classify_nxt_holding",
]
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit.**

```bash
git add app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
feat(rob-23): add NXT classifier service skeleton

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 3 — Implement pending-order classifier: error / non-NXT / unknown / too-far paths

**Files:**
- Modify: `app/services/nxt_classifier_service.py`
- Modify: `tests/services/test_nxt_classifier_service.py`

- [ ] **Step 1: Write the failing tests for error / non-NXT / unknown / too-far classifications.**

Append to `tests/services/test_nxt_classifier_service.py`:
```python
from datetime import UTC, datetime
from decimal import Decimal

from app.services.nxt_classifier_service import (
    NxtClassifierConfig,
    classify_nxt_pending_order,
)
from app.services.pending_reconciliation_service import (
    KrUniverseContext,
    MarketContextInput,
    PendingOrderInput,
    QuoteContext,
    SupportResistanceContext,
    SupportResistanceLevel,
)


def _order(**overrides) -> PendingOrderInput:
    base = {
        "order_id": "O1",
        "symbol": "005930",
        "market": "kr",
        "side": "buy",
        "ordered_price": Decimal("70000"),
        "ordered_qty": Decimal("10"),
        "remaining_qty": Decimal("10"),
        "currency": "KRW",
        "ordered_at": None,
    }
    base.update(overrides)
    return PendingOrderInput(**base)


def _ctx(
    *,
    quote: str | None = None,
    nxt_eligible: bool | None = True,
) -> MarketContextInput:
    kr = (
        None if nxt_eligible is None else KrUniverseContext(nxt_eligible=nxt_eligible)
    )
    return MarketContextInput(
        quote=(QuoteContext(price=Decimal(quote), as_of=None) if quote else None),
        orderbook=None,
        support_resistance=None,
        kr_universe=kr,
    )


@pytest.mark.unit
def test_pending_unknown_venue_maps_to_data_mismatch() -> None:
    item = classify_nxt_pending_order(_order(market="paper"), _ctx())
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert item.kind == "pending_order"


@pytest.mark.unit
def test_pending_data_mismatch_currency_maps_to_data_mismatch() -> None:
    item = classify_nxt_pending_order(_order(currency="USD"), _ctx())
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "currency_mismatch" in item.reasons


@pytest.mark.unit
def test_pending_non_nxt_kr_034220_maps_to_non_nxt_pending_ignore() -> None:
    """ROB-29 fixture: 034220 LG디스플레이 — KR pending, non-NXT.

    KIS inquire-psbl-rvsecncl (TR_ID TTTC8036R) returns this row even though
    the symbol is not NXT-eligible; the classifier must exclude it from
    NXT-actionable outputs.
    """
    item = classify_nxt_pending_order(
        _order(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_pending_kr_missing_universe_fails_closed_to_data_mismatch() -> None:
    """ROB-29 fail-closed: KR pending without a kr_symbol_universe row must
    NEVER fall through to *_actionable / *_at_support. Default-to-actionable
    is a safety regression."""
    # Quote present, S-R missing, kr_universe missing → ROB-22 emits
    # warning "missing_kr_universe" and lets classification proceed; the
    # ROB-23 mapper must override to data_mismatch_requires_review.
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "missing_kr_universe" in item.warnings
    assert "missing_kr_universe_fail_closed" in item.reasons


@pytest.mark.unit
def test_pending_kr_missing_universe_overrides_at_support_attempt() -> None:
    """Even when S-R / quote / orderbook would otherwise yield a strong
    actionable signal, missing kr_universe must dominate."""
    sr = SupportResistanceContext(
        nearest_support=SupportResistanceLevel(
            price=Decimal("70300"), distance_pct=Decimal("0.5")
        ),
        nearest_resistance=None,
    )
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=None,
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_pending_unknown_when_quote_missing() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(quote=None, nxt_eligible=True))
    assert item.classification == "unknown"
    assert item.nxt_actionable is False
    assert "missing_quote" in item.warnings


@pytest.mark.unit
def test_buy_pending_too_far_when_market_through_limit() -> None:
    # ordered 70000, current 80000 → reconciliation says "too_far".
    item = classify_nxt_pending_order(_order(), _ctx(quote="80000"))
    assert item.classification == "buy_pending_too_far"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_sell_pending_too_optimistic_when_market_through_limit() -> None:
    # sell 70000, current 60000 → reconciliation says "too_far".
    item = classify_nxt_pending_order(_order(side="sell"), _ctx(quote="60000"))
    assert item.classification == "sell_pending_too_optimistic"
    assert item.nxt_actionable is False
```

- [ ] **Step 2: Run tests to verify they fail.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: 8 new failures (`NotImplementedError`) — 6 base path tests + 2 ROB-29 fail-closed regression tests.

- [ ] **Step 3: Implement the pending-order classifier (error / non-NXT / unknown / too-far / chasing paths + ROB-29 fail-closed for missing KR universe).**

Replace the `classify_nxt_pending_order` body in `app/services/nxt_classifier_service.py` and add internal helpers. Add the imports to the existing import block:
```python
from app.services.pending_reconciliation_service import (
    KrUniverseContext,
    MarketContextInput,
    OrderbookContext,
    PendingOrderInput,
    PendingReconciliationItem,
    ReconciliationConfig,
    SupportResistanceContext,
    reconcile_pending_order,
)
```

Then replace the function bodies + helpers:
```python
_NXT_ACTIONABLE_LABELS: frozenset[NxtClassification] = frozenset(
    {
        "buy_pending_at_support",
        "buy_pending_actionable",
        "sell_pending_near_resistance",
        "sell_pending_actionable",
    }
)


def _is_nxt_actionable(label: NxtClassification) -> bool:
    return label in _NXT_ACTIONABLE_LABELS


def _map_recon_to_nxt(
    recon: PendingReconciliationItem,
    *,
    market: str,
    side: str,
    order_price: Decimal,
    nxt_cfg: NxtClassifierConfig,
) -> tuple[NxtClassification, list[str]]:
    extra_reasons: list[str] = []
    if recon.classification in ("unknown_venue", "data_mismatch"):
        return "data_mismatch_requires_review", extra_reasons
    if recon.classification == "kr_pending_non_nxt":
        return "non_nxt_pending_ignore_for_nxt", extra_reasons
    # ROB-29 fail-closed: KR pending with no resolvable NXT-eligibility row must
    # NEVER default to actionable. Fires before any quote / S-R rule.
    if market == "kr" and "missing_kr_universe" in recon.warnings:
        extra_reasons.append("missing_kr_universe_fail_closed")
        return "data_mismatch_requires_review", extra_reasons
    if recon.classification == "unknown":
        return "unknown", extra_reasons
    if recon.classification == "too_far":
        return (
            "buy_pending_too_far" if side == "buy" else "sell_pending_too_optimistic"
        ), extra_reasons
    if recon.classification == "chasing_risk":
        return (
            "buy_pending_too_far" if side == "buy" else "sell_pending_too_optimistic"
        ), extra_reasons

    # near_fill or maintain → S/R proximity decides at_support / near_resistance
    if side == "buy":
        support_price = recon.decision_support.get("nearest_support_price")
        if isinstance(support_price, Decimal) and order_price > 0:
            order_to_support_pct = (
                abs(order_price - support_price) / order_price * Decimal("100")
            )
            if order_to_support_pct <= nxt_cfg.near_support_pct:
                extra_reasons.append("order_within_near_support_pct")
                return "buy_pending_at_support", extra_reasons
        return "buy_pending_actionable", extra_reasons

    resistance_price = recon.decision_support.get("nearest_resistance_price")
    if isinstance(resistance_price, Decimal) and order_price > 0:
        order_to_resistance_pct = (
            abs(order_price - resistance_price) / order_price * Decimal("100")
        )
        if order_to_resistance_pct <= nxt_cfg.near_resistance_pct:
            extra_reasons.append("order_within_near_resistance_pct")
            return "sell_pending_near_resistance", extra_reasons
    return "sell_pending_actionable", extra_reasons


def _apply_orderbook_warnings(
    decision_support: dict[str, Decimal | str | None],
    orderbook: OrderbookContext | None,
    nxt_cfg: NxtClassifierConfig,
    warnings: list[str],
) -> None:
    spread = decision_support.get("bid_ask_spread_pct")
    if isinstance(spread, Decimal) and spread > nxt_cfg.wide_spread_pct:
        warnings.append("wide_spread")
    if (
        orderbook is not None
        and nxt_cfg.thin_liquidity_total_qty is not None
    ):
        bid_total = orderbook.total_bid_qty or Decimal("0")
        ask_total = orderbook.total_ask_qty or Decimal("0")
        if bid_total + ask_total < nxt_cfg.thin_liquidity_total_qty:
            warnings.append("thin_liquidity")


def _build_summary(  # filled in Task 8
    classification: NxtClassification,
    decision_support: dict[str, Decimal | str | None],
) -> str:
    return ""  # placeholder until Task 8


def classify_nxt_pending_order(
    order: PendingOrderInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    recon = reconcile_pending_order(
        order, context, config=reconciliation_config, now=now
    )
    classification, extra_reasons = _map_recon_to_nxt(
        recon,
        market=order.market,
        side=order.side,
        order_price=order.ordered_price,
        nxt_cfg=nxt_cfg,
    )
    warnings = list(recon.warnings)
    _apply_orderbook_warnings(
        recon.decision_support, context.orderbook, nxt_cfg, warnings
    )
    return NxtClassifierItem(
        item_id=order.order_id,
        symbol=order.symbol,
        kind="pending_order",
        side=order.side,
        classification=classification,
        nxt_actionable=_is_nxt_actionable(classification),
        summary=_build_summary(classification, recon.decision_support),
        reasons=tuple(list(recon.reasons) + extra_reasons),
        warnings=tuple(warnings),
        decision_support=recon.decision_support,
    )
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: 10 passed (2 from Task 2 + 8 from Task 3, including the 2 ROB-29 fail-closed regressions).

- [ ] **Step 5: Commit.**

```bash
git add app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
feat(rob-23): classify NXT pending-order paths + ROB-29 fail-closed for missing KR universe

Absorbs ROB-29: KIS inquire-psbl-rvsecncl (TR_ID TTTC8036R) returns KR
pending orders regardless of NXT eligibility; non-NXT (e.g. 034220) must
classify as non_nxt_pending_ignore_for_nxt and missing kr_symbol_universe
rows must fail-closed to data_mismatch_requires_review with reason
missing_kr_universe_fail_closed — no actionable default.

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 4 — Implement at-support / near-resistance / actionable rules

**Files:**
- Modify: `tests/services/test_nxt_classifier_service.py`

(Implementation already added in Task 3 — this task adds the tests proving the S/R proximity logic works in both `near_fill` and `maintain` reconciliation cases.)

- [ ] **Step 1: Write the failing tests.**

Append to `tests/services/test_nxt_classifier_service.py`:
```python
from app.services.pending_reconciliation_service import (
    SupportResistanceContext,
    SupportResistanceLevel,
)


def _ctx_with_sr(
    *,
    quote: str,
    support_price: str | None,
    resistance_price: str | None,
    nxt_eligible: bool = True,
) -> MarketContextInput:
    sr = SupportResistanceContext(
        nearest_support=(
            SupportResistanceLevel(
                price=Decimal(support_price), distance_pct=Decimal("0.5")
            )
            if support_price
            else None
        ),
        nearest_resistance=(
            SupportResistanceLevel(
                price=Decimal(resistance_price), distance_pct=Decimal("0.5")
            )
            if resistance_price
            else None
        ),
    )
    return MarketContextInput(
        quote=QuoteContext(price=Decimal(quote), as_of=None),
        orderbook=None,
        support_resistance=sr,
        kr_universe=KrUniverseContext(nxt_eligible=nxt_eligible),
    )


@pytest.mark.unit
def test_buy_pending_at_support_when_order_price_within_near_support_pct() -> None:
    # buy 70000, current 70200 → reconciliation: near_fill.
    # support 70300 → |70000-70300|/70000 = 0.4286% <= 1.0% → at_support.
    ctx = _ctx_with_sr(
        quote="70200", support_price="70300", resistance_price=None
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"
    assert item.nxt_actionable is True
    assert "order_within_near_support_pct" in item.reasons


@pytest.mark.unit
def test_buy_pending_actionable_when_support_far() -> None:
    # buy 70000, current 70200 → reconciliation: near_fill.
    # support 60000 → |70000-60000|/70000 = 14.28% > 1.0% → actionable.
    ctx = _ctx_with_sr(
        quote="70200", support_price="60000", resistance_price=None
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_actionable"
    assert item.nxt_actionable is True


@pytest.mark.unit
def test_buy_pending_actionable_when_no_support_data() -> None:
    item = classify_nxt_pending_order(
        _order(),
        MarketContextInput(
            quote=QuoteContext(price=Decimal("70200"), as_of=None),
            orderbook=None,
            support_resistance=None,
            kr_universe=KrUniverseContext(nxt_eligible=True),
        ),
    )
    assert item.classification == "buy_pending_actionable"
    assert item.nxt_actionable is True
    assert "missing_support_resistance" in item.warnings


@pytest.mark.unit
def test_sell_pending_near_resistance_when_order_price_within_near_resistance_pct() -> None:
    # sell 70000, current 69800 → reconciliation: near_fill (|gap|<=0.5%).
    # resistance 70300 → 0.4286% <= 1.0% → near_resistance.
    ctx = _ctx_with_sr(
        quote="69800", support_price=None, resistance_price="70300"
    )
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_near_resistance"
    assert item.nxt_actionable is True
    assert "order_within_near_resistance_pct" in item.reasons


@pytest.mark.unit
def test_sell_pending_actionable_when_resistance_far() -> None:
    ctx = _ctx_with_sr(
        quote="69800", support_price=None, resistance_price="80000"
    )
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_actionable"


@pytest.mark.unit
def test_buy_pending_at_support_in_maintain_band() -> None:
    # buy 70000, current 68000 → reconciliation: maintain (|gap|=2.857%, not too_far, not chasing).
    # support 69500 → |70000-69500|/70000 = 0.71% <= 1.0% → at_support.
    ctx = _ctx_with_sr(
        quote="68000", support_price="69500", resistance_price=None
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"


@pytest.mark.unit
def test_chasing_risk_buy_maps_to_buy_pending_too_far() -> None:
    # ROB-22 chasing_risk path: buy 70000, current 67000, resistance 67500 (distance_pct 0.5).
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
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_too_far"
    assert item.nxt_actionable is False
```

- [ ] **Step 2: Run tests; verify they pass against the existing implementation from Task 3.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: all 17 tests pass (10 from Tasks 2–3 + 7 from Task 4 — no implementation change needed; the `_map_recon_to_nxt` helper from Task 3 already covers these cases).

- [ ] **Step 3: Commit.**

```bash
git add tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
test(rob-23): cover NXT at-support / near-resistance / actionable / chasing paths

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 5 — Implement candidate classifier (delegate via PendingOrderInput adapter)

**Files:**
- Modify: `app/services/nxt_classifier_service.py`
- Modify: `tests/services/test_nxt_classifier_service.py`

- [ ] **Step 1: Write the failing tests for candidates.**

Append:
```python
from app.services.nxt_classifier_service import (
    NxtCandidateInput,
    classify_nxt_candidate,
)


def _candidate(**overrides) -> NxtCandidateInput:
    base = {
        "candidate_id": "C1",
        "symbol": "005930",
        "side": "buy",
        "proposed_price": Decimal("70000"),
        "proposed_qty": Decimal("10"),
        "currency": "KRW",
    }
    base.update(overrides)
    return NxtCandidateInput(**base)


@pytest.mark.unit
def test_candidate_buy_at_support() -> None:
    ctx = _ctx_with_sr(
        quote="70200", support_price="70300", resistance_price=None
    )
    item = classify_nxt_candidate(_candidate(), ctx)
    assert item.kind == "candidate"
    assert item.classification == "buy_pending_at_support"
    assert item.nxt_actionable is True


@pytest.mark.unit
def test_candidate_sell_too_optimistic() -> None:
    item = classify_nxt_candidate(
        _candidate(side="sell"), _ctx(quote="60000")
    )
    assert item.classification == "sell_pending_too_optimistic"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_non_nxt_034220_excluded() -> None:
    """ROB-29 fixture parity for candidates: 034220 LG디스플레이."""
    item = classify_nxt_candidate(
        _candidate(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_kr_missing_universe_fails_closed() -> None:
    """ROB-29 fail-closed must apply to candidates as well as pending orders."""
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=None,
        support_resistance=None,
        kr_universe=None,
    )
    item = classify_nxt_candidate(_candidate(), ctx)
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False
    assert "missing_kr_universe_fail_closed" in item.reasons


@pytest.mark.unit
def test_candidate_data_mismatch_currency() -> None:
    item = classify_nxt_candidate(_candidate(currency="USD"), _ctx(quote="70200"))
    assert item.classification == "data_mismatch_requires_review"
    assert item.nxt_actionable is False


@pytest.mark.unit
def test_candidate_unknown_when_quote_missing() -> None:
    item = classify_nxt_candidate(_candidate(), _ctx(nxt_eligible=True))
    assert item.classification == "unknown"
    assert "missing_quote" in item.warnings


@pytest.mark.unit
def test_candidate_with_no_proposed_qty_still_classifies() -> None:
    # Candidates may not specify a quantity. The adapter substitutes Decimal("1")
    # so the reconciliation service does not flag non_positive_remaining_qty.
    ctx = _ctx_with_sr(
        quote="70200", support_price="70300", resistance_price=None
    )
    item = classify_nxt_candidate(_candidate(proposed_qty=None), ctx)
    assert item.classification == "buy_pending_at_support"
    assert "non_positive_remaining_qty" not in item.reasons
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: 7 new failures (`NotImplementedError`) — 6 base candidate tests + the ROB-29 candidate fail-closed regression.

- [ ] **Step 3: Implement `classify_nxt_candidate`.**

Replace the body in `app/services/nxt_classifier_service.py`:
```python
def classify_nxt_candidate(
    candidate: NxtCandidateInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
    reconciliation_config: ReconciliationConfig | None = None,
    now: datetime | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    proxy_qty = (
        candidate.proposed_qty
        if candidate.proposed_qty is not None and candidate.proposed_qty > 0
        else Decimal("1")
    )
    proxy_order = PendingOrderInput(
        order_id=candidate.candidate_id,
        symbol=candidate.symbol,
        market="kr",
        side=candidate.side,
        ordered_price=candidate.proposed_price,
        ordered_qty=proxy_qty,
        remaining_qty=proxy_qty,
        currency=candidate.currency,
        ordered_at=None,
    )
    recon = reconcile_pending_order(
        proxy_order, context, config=reconciliation_config, now=now
    )
    classification, extra_reasons = _map_recon_to_nxt(
        recon,
        market="kr",
        side=candidate.side,
        order_price=candidate.proposed_price,
        nxt_cfg=nxt_cfg,
    )
    warnings = list(recon.warnings)
    _apply_orderbook_warnings(
        recon.decision_support, context.orderbook, nxt_cfg, warnings
    )
    return NxtClassifierItem(
        item_id=candidate.candidate_id,
        symbol=candidate.symbol,
        kind="candidate",
        side=candidate.side,
        classification=classification,
        nxt_actionable=_is_nxt_actionable(classification),
        summary=_build_summary(classification, recon.decision_support),
        reasons=tuple(list(recon.reasons) + extra_reasons),
        warnings=tuple(warnings),
        decision_support=recon.decision_support,
    )
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
feat(rob-23): classify NXT candidates by adapting to reconciliation pipeline

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 6 — Implement holding classifier (watch-only / non-NXT / data-mismatch)

**Files:**
- Modify: `app/services/nxt_classifier_service.py`
- Modify: `tests/services/test_nxt_classifier_service.py`

- [ ] **Step 1: Write the failing tests.**

Append:
```python
from app.services.nxt_classifier_service import (
    NxtHoldingInput,
    classify_nxt_holding,
)


def _holding(**overrides) -> NxtHoldingInput:
    base = {
        "holding_id": "H1",
        "symbol": "005930",
        "quantity": Decimal("10"),
        "currency": "KRW",
    }
    base.update(overrides)
    return NxtHoldingInput(**base)


@pytest.mark.unit
def test_holding_watch_only_when_nxt_eligible() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=True))
    assert item.kind == "holding"
    assert item.classification == "holding_watch_only"
    assert item.nxt_actionable is False
    assert item.side is None


@pytest.mark.unit
def test_holding_non_nxt_excluded() -> None:
    item = classify_nxt_holding(
        _holding(symbol="034220"), _ctx(nxt_eligible=False)
    )
    assert item.classification == "non_nxt_pending_ignore_for_nxt"
    assert item.nxt_actionable is False
    assert "non_nxt_venue" in item.warnings


@pytest.mark.unit
def test_holding_missing_kr_universe_falls_back_to_watch_only_with_warning() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=None))
    assert item.classification == "holding_watch_only"
    assert "missing_kr_universe" in item.warnings


@pytest.mark.unit
def test_holding_data_mismatch_non_positive_quantity() -> None:
    item = classify_nxt_holding(
        _holding(quantity=Decimal("0")), _ctx(nxt_eligible=True)
    )
    assert item.classification == "data_mismatch_requires_review"
    assert "non_positive_quantity" in item.reasons


@pytest.mark.unit
def test_holding_data_mismatch_currency() -> None:
    item = classify_nxt_holding(
        _holding(currency="USD"), _ctx(nxt_eligible=True)
    )
    assert item.classification == "data_mismatch_requires_review"
    assert "currency_mismatch" in item.reasons
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: 5 new failures (`NotImplementedError`).

- [ ] **Step 3: Implement `classify_nxt_holding`.**

Replace the function body and add a small helper for decision-support population:
```python
def _holding_decision_support(
    context: MarketContextInput,
) -> dict[str, Decimal | str | None]:
    ds: dict[str, Decimal | str | None] = {
        "current_price": None,
        "gap_pct": None,
        "signed_distance_to_fill": None,
        "nearest_support_price": None,
        "nearest_support_distance_pct": None,
        "nearest_resistance_price": None,
        "nearest_resistance_distance_pct": None,
        "bid_ask_spread_pct": None,
    }
    if context.quote is not None:
        ds["current_price"] = context.quote.price
    sr = context.support_resistance
    if sr is not None:
        if sr.nearest_support is not None:
            ds["nearest_support_price"] = sr.nearest_support.price
            ds["nearest_support_distance_pct"] = sr.nearest_support.distance_pct
        if sr.nearest_resistance is not None:
            ds["nearest_resistance_price"] = sr.nearest_resistance.price
            ds["nearest_resistance_distance_pct"] = sr.nearest_resistance.distance_pct
    ob = context.orderbook
    if ob is not None and ob.best_bid is not None and ob.best_ask is not None:
        bid = ob.best_bid.price
        ask = ob.best_ask.price
        if bid > 0 and ask > 0:
            ds["bid_ask_spread_pct"] = (
                (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
            )
    return ds


def classify_nxt_holding(
    holding: NxtHoldingInput,
    context: MarketContextInput,
    *,
    config: NxtClassifierConfig | None = None,
) -> NxtClassifierItem:
    nxt_cfg = config or NxtClassifierConfig()
    reasons: list[str] = []
    warnings: list[str] = []
    decision_support = _holding_decision_support(context)

    if holding.quantity is None or holding.quantity <= 0:
        reasons.append("non_positive_quantity")
    if holding.currency and holding.currency.upper() != "KRW":
        reasons.append("currency_mismatch")
    if reasons:
        return NxtClassifierItem(
            item_id=holding.holding_id,
            symbol=holding.symbol,
            kind="holding",
            side=None,
            classification="data_mismatch_requires_review",
            nxt_actionable=False,
            summary=_build_summary(
                "data_mismatch_requires_review", decision_support
            ),
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            decision_support=decision_support,
        )

    if context.kr_universe is None:
        warnings.append("missing_kr_universe")
        classification: NxtClassification = "holding_watch_only"
    elif not context.kr_universe.nxt_eligible:
        warnings.append("non_nxt_venue")
        classification = "non_nxt_pending_ignore_for_nxt"
    else:
        classification = "holding_watch_only"

    _apply_orderbook_warnings(
        decision_support, context.orderbook, nxt_cfg, warnings
    )

    return NxtClassifierItem(
        item_id=holding.holding_id,
        symbol=holding.symbol,
        kind="holding",
        side=None,
        classification=classification,
        nxt_actionable=False,
        summary=_build_summary(classification, decision_support),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        decision_support=decision_support,
    )
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
feat(rob-23): classify NXT holdings as watch-only / non-NXT / data-mismatch

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 7 — Spread / liquidity warnings (orderbook context)

**Files:**
- Modify: `tests/services/test_nxt_classifier_service.py`

(Implementation already added in Task 3 via `_apply_orderbook_warnings` and called from all three classifier entry points. This task locks the behavior with explicit tests.)

- [ ] **Step 1: Write the failing tests.**

Append:
```python
from app.services.pending_reconciliation_service import (
    OrderbookContext,
    OrderbookLevelContext,
)


def _ob(
    *,
    bid_price: str,
    ask_price: str,
    bid_qty: str = "100",
    ask_qty: str = "100",
    total_bid_qty: str | None = None,
    total_ask_qty: str | None = None,
) -> OrderbookContext:
    return OrderbookContext(
        best_bid=OrderbookLevelContext(
            price=Decimal(bid_price), quantity=Decimal(bid_qty)
        ),
        best_ask=OrderbookLevelContext(
            price=Decimal(ask_price), quantity=Decimal(ask_qty)
        ),
        total_bid_qty=Decimal(total_bid_qty) if total_bid_qty is not None else None,
        total_ask_qty=Decimal(total_ask_qty) if total_ask_qty is not None else None,
    )


@pytest.mark.unit
def test_wide_spread_warning_emitted_above_threshold() -> None:
    # bid 69500, ask 70500 → spread = 1000 / 70000 = 1.4286% > 1.0% (default threshold).
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69500", ask_price="70500"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "wide_spread" in item.warnings


@pytest.mark.unit
def test_wide_spread_not_emitted_below_threshold() -> None:
    # bid 69900, ask 70100 → spread = 200 / 70000 ≈ 0.286% < 1%.
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69900", ask_price="70100"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "wide_spread" not in item.warnings


@pytest.mark.unit
def test_thin_liquidity_warning_when_threshold_set() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(
            bid_price="70100",
            ask_price="70200",
            total_bid_qty="50",
            total_ask_qty="40",
        ),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(
        _order(),
        ctx,
        config=NxtClassifierConfig(thin_liquidity_total_qty=Decimal("200")),
    )
    assert "thin_liquidity" in item.warnings


@pytest.mark.unit
def test_thin_liquidity_warning_skipped_when_threshold_none() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(
            bid_price="70100",
            ask_price="70200",
            total_bid_qty="1",
            total_ask_qty="1",
        ),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert "thin_liquidity" not in item.warnings


@pytest.mark.unit
def test_holding_emits_wide_spread_warning_too() -> None:
    ctx = MarketContextInput(
        quote=QuoteContext(price=Decimal("70200"), as_of=None),
        orderbook=_ob(bid_price="69500", ask_price="70500"),
        support_resistance=None,
        kr_universe=KrUniverseContext(nxt_eligible=True),
    )
    item = classify_nxt_holding(_holding(), ctx)
    assert "wide_spread" in item.warnings
```

- [ ] **Step 2: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: all tests pass.

- [ ] **Step 3: Commit.**

```bash
git add tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
test(rob-23): assert spread / thin-liquidity warnings on NXT classifier

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 8 — Operator-facing summary templates

**Files:**
- Modify: `app/services/nxt_classifier_service.py`
- Modify: `tests/services/test_nxt_classifier_service.py`

- [ ] **Step 1: Write the failing tests.**

Append:
```python
@pytest.mark.unit
def test_summary_buy_pending_at_support_includes_support_price() -> None:
    ctx = _ctx_with_sr(
        quote="70200", support_price="70300", resistance_price=None
    )
    item = classify_nxt_pending_order(_order(), ctx)
    assert item.classification == "buy_pending_at_support"
    assert "지지선" in item.summary
    assert "70300" in item.summary


@pytest.mark.unit
def test_summary_sell_pending_near_resistance_includes_resistance_price() -> None:
    ctx = _ctx_with_sr(
        quote="69800", support_price=None, resistance_price="70300"
    )
    item = classify_nxt_pending_order(_order(side="sell"), ctx)
    assert item.classification == "sell_pending_near_resistance"
    assert "저항선" in item.summary
    assert "70300" in item.summary


@pytest.mark.unit
def test_summary_buy_pending_too_far_uses_review_template() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(quote="80000"))
    assert "재검토" in item.summary


@pytest.mark.unit
def test_summary_non_nxt_pending_uses_exclude_template() -> None:
    item = classify_nxt_pending_order(
        _order(symbol="034220"), _ctx(quote="9800", nxt_eligible=False)
    )
    assert "NXT" in item.summary
    assert "제외" in item.summary


@pytest.mark.unit
def test_summary_holding_watch_only_template() -> None:
    item = classify_nxt_holding(_holding(), _ctx(nxt_eligible=True))
    assert "보유" in item.summary
    assert "모니터링" in item.summary


@pytest.mark.unit
def test_summary_data_mismatch_template() -> None:
    item = classify_nxt_pending_order(_order(currency="USD"), _ctx())
    assert "데이터" in item.summary
    assert "검토" in item.summary


@pytest.mark.unit
def test_summary_unknown_template() -> None:
    item = classify_nxt_pending_order(_order(), _ctx(nxt_eligible=True))
    assert "분류 불가" in item.summary
```

- [ ] **Step 2: Run tests; verify they fail.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: failures because `_build_summary` returns `""`.

- [ ] **Step 3: Implement `_build_summary`.**

Replace the placeholder `_build_summary` in `app/services/nxt_classifier_service.py`:
```python
def _format_price(value: object) -> str:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return ""


def _build_summary(
    classification: NxtClassification,
    decision_support: dict[str, Decimal | str | None],
) -> str:
    if classification == "buy_pending_at_support":
        price = _format_price(decision_support.get("nearest_support_price"))
        if price:
            return f"NXT 매수 대기 — 지지선 근접 (지지선 {price})"
        return "NXT 매수 대기 — 적정 (지속 모니터링)"
    if classification == "buy_pending_actionable":
        return "NXT 매수 대기 — 적정 (지속 모니터링)"
    if classification == "buy_pending_too_far":
        return "NXT 매수 대기 — 시장가 대비 이격 큼 (재검토 필요)"
    if classification == "sell_pending_near_resistance":
        price = _format_price(decision_support.get("nearest_resistance_price"))
        if price:
            return f"NXT 매도 대기 — 저항선 근접 (저항선 {price})"
        return "NXT 매도 대기 — 적정 (지속 모니터링)"
    if classification == "sell_pending_actionable":
        return "NXT 매도 대기 — 적정 (지속 모니터링)"
    if classification == "sell_pending_too_optimistic":
        return "NXT 매도 대기 — 시장가 대비 너무 낙관적 (재검토 필요)"
    if classification == "non_nxt_pending_ignore_for_nxt":
        return "KR 일반종목 — NXT 대상 아님 (NXT 의사결정에서 제외)"
    if classification == "holding_watch_only":
        return "NXT 보유 — 신규 액션 없음, 모니터링 대상"
    if classification == "data_mismatch_requires_review":
        return "주문/포지션 데이터 불일치 — 운영자 검토 필요"
    return "NXT 분류 불가 — 시세 정보 부족"
```

- [ ] **Step 4: Run tests; verify they pass.**

Run: `uv run pytest tests/services/test_nxt_classifier_service.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit.**

```bash
git add app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
git commit -m "$(cat <<'EOF'
feat(rob-23): add operator-facing Korean summaries to NXT classifier

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 9 — Safety: forbid broker / order / DB / Redis transitive imports

**Files:**
- Create: `tests/services/test_nxt_classifier_service_safety.py`

- [ ] **Step 1: Write the test.**

Write the file:
```python
"""Safety: NXT classifier service must stay pure.

Modeled on tests/services/test_pending_reconciliation_service_safety.py — runs
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
def test_nxt_classifier_service_is_pure() -> None:
    loaded = _loaded_modules_after_import("app.services.nxt_classifier_service")
    violations = sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")
```

- [ ] **Step 2: Run the test; verify it passes.**

Run: `uv run pytest tests/services/test_nxt_classifier_service_safety.py -v`
Expected: pass. If it fails, the implementation accidentally imported a forbidden module — investigate the import in `app/services/nxt_classifier_service.py` (it should use only `dataclasses`, `datetime`, `decimal`, `typing`, `collections.abc`, and `app.services.pending_reconciliation_service`, which is itself proven pure by ROB-22's safety test).

- [ ] **Step 3: Commit.**

```bash
git add tests/services/test_nxt_classifier_service_safety.py
git commit -m "$(cat <<'EOF'
test(rob-23): enforce NXT classifier service has no broker / DB imports

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 10 — Lint, typecheck, full test sweep

**Files:** none

- [ ] **Step 1: Run lint.**

Run: `make lint`
Expected: clean. If Ruff complains about cognitive complexity in `_map_recon_to_nxt` or `_build_summary`, suppress with `# noqa: C901` and a one-line comment explaining why ("rule-by-rule mapper").

- [ ] **Step 2: Run typecheck.**

Run: `make typecheck`
Expected: no new errors in `app/services/nxt_classifier_service.py` or its test files.

- [ ] **Step 3: Run the unit-test scope.**

Run: `make test-unit`
Expected: all green; no regressions introduced. Spot-check that `tests/services/test_pending_reconciliation_service*.py` still pass — they should, since ROB-23 does not modify the reconciliation module.

- [ ] **Step 4: Commit any lint/format-only changes (if any).**

```bash
git status
# If only auto-formatter changes appear:
git add -p
git commit -m "$(cat <<'EOF'
chore(rob-23): apply ruff formatting

Co-Authored-By: Paperclip <noreply@paperclip.ing>
EOF
)"
```

---

### Task 11 — Open the PR

**Files:** none

- [ ] **Step 1: Push the branch.**

```bash
git push -u origin feature/ROB-23-nxt-order-candidate-classifier
```

- [ ] **Step 2: Open PR against `main`.**

```bash
gh pr create --base main --title "ROB-23: Add NXT-specific order/candidate/holding classifier (absorbs ROB-29)" --body "$(cat <<'EOF'
## Summary
- Add `app/services/nxt_classifier_service.py`: pure NXT-specific classifier built on top of ROB-22's `pending_reconciliation_service`. Handles pending orders, candidates (proposed orders), and holdings.
- Classifications: `buy_pending_at_support`, `buy_pending_too_far`, `buy_pending_actionable`, `sell_pending_near_resistance`, `sell_pending_too_optimistic`, `sell_pending_actionable`, `non_nxt_pending_ignore_for_nxt`, `holding_watch_only`, `data_mismatch_requires_review`, `unknown`.
- Warnings: propagates ROB-22's reconciliation warnings; adds `wide_spread` and `thin_liquidity` (when an `OrderbookContext` is present and the relevant config threshold is set).
- Operator-facing Korean summary string per classification, suitable for proposal-card rendering.
- KR/NXT awareness via the caller-supplied `KrUniverseContext` (caller resolves it via `KrSymbolUniverseService.is_nxt_eligible`); non-NXT KR symbols (e.g. 034220 LG디스플레이) classify as `non_nxt_pending_ignore_for_nxt` with `nxt_actionable=false`.

## Absorbs ROB-29
- KIS pending source documented: `get_order_history(status="pending", market="kr")` calls KIS `/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl` (TR_ID `TTTC8036R`, 국내주식 정정취소가능주문조회). Not an NXT-only list — broker may return any KR pending it considers modifiable/cancellable.
- Per-row NXT eligibility is enforced by consulting `kr_symbol_universe.nxt_eligible` for every KR pending / candidate fed into the classifier. Non-NXT (e.g. 034220) are excluded from NXT-actionable outputs and from any downstream NXT modify/cancel/execution candidate consumer.
- Fail-closed for missing KR universe rows: classifier returns `data_mismatch_requires_review` with `nxt_actionable=false` and reason `missing_kr_universe_fail_closed` instead of any actionable default.
- No separate ROB-29 worktree was opened; ROB-29 ships inside this PR.

## Trading-safety invariants
- Pure function module; no `place_order` / `modify_order` / `cancel_order` / watch-alert / paper / dry-run / fill-notification / broker / DB / Redis imports.
- Subprocess `sys.modules` test (`tests/services/test_nxt_classifier_service_safety.py`) enforces the import isolation, modeled on ROB-22's safety test.
- No new mutation paths or order side effects. Decision Session creation is unaffected. TradingAgents is not invoked here; if a follow-up wires it in, it must remain `advisory_only=true / execution_allowed=false`.

## Out of scope
- ROB-20 live refresh wiring / UI rendering: caller-side concerns.
- ROB-25 (or later) wiring follow-up: connecting this classifier into `operator_decision_session_service`, `tradingagents_research_service`, Decision Session proposal generation, dashboards, Prefect flows.
- API endpoint, Prefect flow, dashboard.
- Persisting classifier results to the DB.

## Test plan
- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `uv run pytest tests/services/test_nxt_classifier_service.py -v`
- [ ] `uv run pytest tests/services/test_nxt_classifier_service_safety.py -v`
- [ ] `uv run pytest tests/services/test_pending_reconciliation_service.py tests/services/test_pending_reconciliation_service_safety.py -v` (regression check)
- [ ] `make test-unit`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Capture the PR URL in the AoE session log.**

---

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Future refactor accidentally imports a broker / DB / Redis module into `nxt_classifier_service.py`. | `tests/services/test_nxt_classifier_service_safety.py` runs in a subprocess and fails the build on any forbidden prefix. |
| Decimal/float coercion bugs distort thresholds. | DTOs use `Decimal` throughout; tests use `Decimal` literals; thresholds are `Decimal` constants in `NxtClassifierConfig`. |
| Misclassifying KR non-NXT as actionable. | Reconciliation rule 3 (`kr_pending_non_nxt`) fires before any quote-dependent rule; the NXT mapper short-circuits before S/R proximity logic. Tested explicitly in Tasks 3 and 5 (034220 fixture) and Task 6 for holdings. |
| Default-to-actionable when KR universe row is missing (ROB-29 regression). | Mapper rule 4 fail-closes to `data_mismatch_requires_review` whenever `market == "kr"` and `missing_kr_universe` is in the reconciliation warnings. Tested in Task 3 (`test_pending_kr_missing_universe_fails_closed_to_data_mismatch`, `test_pending_kr_missing_universe_overrides_at_support_attempt`) and Task 5 (`test_candidate_kr_missing_universe_fails_closed`). The reason `missing_kr_universe_fail_closed` makes the override discoverable in audit logs. |
| KR pending source assumption drifts (someone treats KIS inquire-psbl-rvsecncl as NXT-only). | Domain Reference documents TR_ID `TTTC8036R` and the modify/cancel-eligible semantics explicitly. Mapper still consults per-row `nxt_eligible`; tests pin the contract. |
| Candidate adapter trips reconciliation's `non_positive_remaining_qty` check when `proposed_qty` is `None`. | Adapter substitutes `Decimal("1")` for missing/zero quantity. Covered by `test_candidate_with_no_proposed_qty_still_classifies`. |
| Order-price-to-S/R distance computed differently from ROB-22's current-price-to-S/R distance. | Implementation explicitly recomputes from `order.ordered_price` and `nearest_*_price`; documented in plan rule 7 and tested in Task 4. |
| Korean summary format drift breaks downstream UI. | Tests (Task 8) assert the *substring* tokens (`지지선`, `저항선`, `재검토`, `제외`, `보유`, `모니터링`, `데이터`, `검토`, `분류 불가`) rather than exact strings; UI consumers should treat the string as opaque. |
| ROB-23 work creeps into ROB-20 / ROB-25 wiring. | Plan calls out: do not add API endpoints, UI templates, Prefect flows, or persistence. Reviewer should reject any new caller in this PR. Consumer wiring (live-refresh integration, Decision Session proposal generation) is deferred to ROB-25 or a later wiring follow-up. |
| ROB-29 spawning a separate worktree / PR. | Explicit "Absorbs ROB-29" header at the top of this plan and in the ROB-29 absorption note states that ROB-29 classifier-level behavior ships inside this PR; no separate ROB-29 implementation worktree is to be opened. |
| Broker side-effect introduced via new caller. | None added in this PR. Safety test enforces purity. PR scope checklist below explicitly disallows it. |

## PR scope (reviewer checklist)

- Adds: `app/services/nxt_classifier_service.py`, `tests/services/test_nxt_classifier_service.py`, `tests/services/test_nxt_classifier_service_safety.py`, `docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md`.
- Does **not** modify: `app/services/pending_reconciliation_service.py`, `app/services/trading_decision_service.py`, `app/services/operator_decision_session_service.py`, `app/services/tradingagents_research_service.py`, `app/services/kr_symbol_universe_service.py`, `app/services/kis*`, `app/services/n8n_pending_*`, `app/mcp_server/tooling/orders_*`, models, schemas, alembic, routers, Prefect flows, UI templates.
- No new env vars, no new dependencies in `pyproject.toml` / `uv.lock`.
- No DB migrations.
- No broker mutation, no watch alert registration, no paper/dry-run/live order placement.
- No secrets, API keys, tokens, or account numbers read or printed.
