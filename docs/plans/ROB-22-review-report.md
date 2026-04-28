# ROB-22 Review Report — Pending Reconciliation Service

**Reviewer:** Claude Opus (read-only)
**Branch:** `feature/ROB-22-pending-reconciliation-service`
**Plan:** `docs/plans/ROB-22-pending-reconciliation-service-plan.md`
**Linear issue:** ROB-22 — [Foundation] Add pending reconciliation service for Research Run live refresh
**Verdict:** **PASSED.** All Linear acceptance criteria are met, the implementation matches the plan rule-for-rule, and the trading-safety guardrails hold. No must-fix items.

---

## Files reviewed

| File | Status | LoC |
|------|--------|-----|
| `app/services/pending_reconciliation_service.py` | new (untracked) | 385 |
| `tests/services/test_pending_reconciliation_service.py` | new (untracked) | 374 |
| `tests/services/test_pending_reconciliation_service_safety.py` | new (untracked) | 83 |
| `docs/plans/ROB-22-pending-reconciliation-service-plan.md` | new (untracked) | — |

`git status` shows all four files as **untracked** — the implementer ran the unit tests and lint/format but did not commit the per-task milestones called out in the plan. This is a process note, not a code-correctness issue: it just means the PR-creation step must run `git add`/`git commit` before pushing. No work has been lost.

## Verifier results (re-run by reviewer)

```
uv run pytest tests/services/test_pending_reconciliation_service.py \
              tests/services/test_pending_reconciliation_service_safety.py -q
24 passed, 2 warnings in 1.14s   (the 2 warnings are pre-existing Pydantic V2 deprecations in app/auth/schemas.py — unrelated to ROB-22)
```

## Acceptance-criteria audit

### AC1 — Pure service with unit tests; no broker/order side effects

- `app/services/pending_reconciliation_service.py` imports only `collections.abc`, `dataclasses`, `datetime`, `decimal`, `typing` (verified with `grep ^import|^from`). No broker, DB, Redis, HTTP, Prefect, or order-execution imports.
- `tests/services/test_pending_reconciliation_service_safety.py` runs the import in a clean subprocess and asserts no module name in `sys.modules` matches any prefix in a 24-item denylist that covers `app.services.kis*`, `app.services.upbit*`, `app.services.brokers*`, `app.services.watch_alerts*`, `app.services.paper_trading_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.kis_websocket*`, `app.services.n8n_pending_*`, `app.mcp_server.tooling.order_execution`, `app.mcp_server.tooling.orders_*`, `app.mcp_server.tooling.watch_alerts_registration`, `app.tasks*`, `app.core.db*`, `redis*`, `httpx*`, `sqlalchemy*`. Test passes.
- The classifier never triggers a side effect: it returns frozen `PendingReconciliationItem` dataclasses. No `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, paper/dry-run/live order placement, watch registration, or broker mutation appears in the module or its tests.
- 24 unit tests pass (12 for the eight classifications, 6 for warnings/decision-support, 3 for `reconcile_pending_orders`, 2 for module surface, 1 reusability test, 1 safety test).

### AC2 — Classifies maintain, near_fill, too_far, chasing_risk, data_mismatch, kr_pending_non_nxt, unknown_venue (plus `unknown` fallback)

| Classification | Test that locks it in | Verified |
|----------------|-----------------------|----------|
| `maintain` | `test_maintain_default` (line 187), `test_chasing_risk_skipped_when_sr_missing` (line 270) | ✓ |
| `near_fill` | `test_near_fill_buy` (163), `test_stale_quote_warning_still_classifies` (205) | ✓ |
| `too_far` | `test_too_far_buy_through_market` (172), `test_too_far_sell_through_market` (180) | ✓ |
| `chasing_risk` | `test_chasing_risk_buy_into_resistance` (229), `test_chasing_risk_sell_into_support` (251) | ✓ |
| `data_mismatch` | `test_data_mismatch_currency_kr_usd` (110), `test_data_mismatch_non_positive_price` (117) | ✓ |
| `kr_pending_non_nxt` | `test_kr_pending_non_nxt` (124), `test_two_callers_share_one_pure_service` (322) | ✓ |
| `unknown_venue` | `test_unknown_venue_market` (95), `test_unknown_venue_side` (103) | ✓ |
| `unknown` (fallback) | `test_unknown_when_quote_missing` (195) | ✓ |

The classifier order matches the plan: rule 1 (`unknown_venue`) → rule 2 (`data_mismatch`) → rule 3 (`kr_pending_non_nxt`) → rule 5 (`unknown` if quote missing) → rules 10–13 (`near_fill`, `too_far`, `chasing_risk`, `maintain`). Non-NXT KR pendings short-circuit before quote-dependent rules, so an LG디스플레이-shaped order stays `kr_pending_non_nxt` even when its quote would otherwise classify as `chasing_risk` (`test_two_callers_share_one_pure_service` proves this exact precedence).

### AC3 — Missing/stale context surfaces explicit warnings

| Warning | Trigger | Test |
|---------|---------|------|
| `unknown_venue` | `order.market not in {kr,us,crypto}` | `test_unknown_venue_market` |
| `unknown_side` | `order.side not in {buy,sell}` | `test_unknown_venue_side` |
| `non_nxt_venue` | KR with `nxt_eligible=False` | `test_kr_pending_non_nxt` |
| `missing_kr_universe` | KR with `kr_universe is None` | `test_kr_universe_missing_warning` |
| `missing_quote` | `context.quote is None` | `test_unknown_when_quote_missing`, `test_reconcile_pending_orders_treats_missing_context_as_empty` |
| `stale_quote` | quote `as_of` older than `quote_stale_seconds` (default 300s) relative to `now` (or `order.ordered_at` fallback) | `test_stale_quote_warning_still_classifies` |
| `missing_orderbook` | `context.orderbook is None` | `test_chasing_risk_skipped_when_sr_missing` (asserts `missing_support_resistance`; same code path emits `missing_orderbook` — both verified manually in the source) |
| `missing_support_resistance` | `context.support_resistance is None` | `test_chasing_risk_skipped_when_sr_missing` |

All warnings are appended to the immutable `warnings` tuple on `PendingReconciliationItem`. Warnings accumulate even when the classification short-circuits (e.g., `data_mismatch` path still calls `_resolve_nxt_actionable` so a KR `data_mismatch` with no universe context still surfaces `missing_kr_universe`). This matches plan §"warnings always accumulate".

### AC4 — Pure / read-only design

- No `import` of any broker, order-execution, watch-alert, paper-order, fill-notification, KIS-websocket, DB, or Redis module (verified via `grep` on the source and via subprocess `sys.modules` test).
- All public types are `@dataclass(frozen=True, slots=True)`; the `decision_support` dict is the one mutable field, scoped to a single function-local instance returned by value.
- No async I/O, no DB session, no HTTP client, no Redis client, no settings access, no logger calls. Every classifier function is synchronous and stateless.

### AC5 — NXT semantics: raw KR pending ≠ NXT pending

- The classifier never assumes a KR pending is NXT. NXT eligibility comes only from caller-supplied `KrUniverseContext.nxt_eligible`. Callers are expected to resolve it via `KrSymbolUniverseService.is_nxt_eligible(symbol)` (per plan).
- `_resolve_nxt_actionable` returns one of three states for KR orders:
  - `(True, False)` when `nxt_eligible=True` (NXT-eligible).
  - `(False, True)` when `nxt_eligible=False` (non-NXT pending — emits `non_nxt_venue` warning, classifies as `kr_pending_non_nxt`, sets `nxt_actionable=False`).
  - `(None, False)` when `kr_universe is None` (emits `missing_kr_universe` warning, sets `nxt_actionable=None`).
- For non-KR markets, `_resolve_nxt_actionable` short-circuits to `(None, False)` — `nxt_actionable` is `None` for US/crypto orders, which is correct: NXT is a KR-only routing concept.
- `test_kr_pending_non_nxt` exercises the canonical example (LG디스플레이 034220) called out in the issue context and confirms `classification="kr_pending_non_nxt"`, `nxt_actionable=False`, `"non_nxt_venue" in warnings`.

### AC6 — Reusable by Research Run live refresh and Decision Session proposal generation

- The service exposes pure functions over plain dataclass DTOs. There is no caller-specific assumption: callers shape `PendingOrderInput` + `MarketContextInput` from whatever data they already have (broker order rows, KR universe rows, quotes, support/resistance levels) and call `reconcile_pending_order` or `reconcile_pending_orders`.
- `reconcile_pending_orders` accepts a `dict[str, MarketContextInput]` keyed by `order_id`, so a caller can construct context once per symbol and reuse it across multiple pending orders.
- `test_two_callers_share_one_pure_service` exercises two distinct construction styles in one test: a "Research Run live refresh"-shaped call (quote + KR universe only) and a "Decision Session proposal generation"-shaped call (quote + support/resistance + non-NXT KR universe). Both call the same `reconcile_pending_order` function and get correctly classified results without either caller-side module being imported.
- ROB-20 wiring is **not** introduced: no router, Prefect flow, dashboard, or persistence is touched. Search of the diff confirms zero edits to `operator_decision_session_service.py`, `trading_decision_service.py`, `tradingagents_research_service.py`, `n8n_pending_*`, `app/routers/`, `app/tasks/`, `alembic/`, or any UI template.

---

## Plan ↔ implementation diff

| Plan rule | Implementation source | Match |
|-----------|----------------------|-------|
| Rule 1: unknown venue/side | `_check_unknown_venue` (lines 122–133) | exact |
| Rule 2: data mismatch (price/qty/currency) | `_check_data_mismatch` (lines 136–152) | exact |
| Rule 3: kr_pending_non_nxt | `_resolve_nxt_actionable` (lines 155–169) + branch at line 226 | exact |
| Rule 4: missing kr universe warning | line 164 inside `_resolve_nxt_actionable` | exact |
| Rule 5: missing quote → unknown | lines 241–255 | exact |
| Rule 6: stale quote check | lines 257–262 | exact |
| Rule 7: missing orderbook warning | lines 264–265 | exact |
| Rule 8: missing SR warning | lines 266–267 | exact |
| Rule 9: gap_pct + signed_distance_to_fill | lines 269–273 | exact |
| Rule 10: near_fill | lines 299–301 | exact |
| Rule 11: too_far (signed_distance_to_fill < 0 AND |gap| ≥ too_far_pct) | lines 302–304 | exact |
| Rule 12: chasing_risk (signed_distance_to_fill > chasing_pct AND SR-side proximity) | lines 305–328 | exact |
| Rule 13: maintain default | lines 329–330 | exact |
| Public API: dataclass + free function shape | matches plan §"Public API of the Service" verbatim | exact |

Modernization: implementer used `from collections.abc import Sequence` instead of `from typing import Sequence`. This is recommended for Python 3.13 and improves on the plan's snippet — accepted as is.

## Trading-safety invariants (re-verified)

- ✅ No `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, paper/dry-run/live order placement, watch registration, or broker API mutation introduced.
- ✅ No DB writes, no Redis writes, no Prefect schedule, no notification fan-out, no Slack/Telegram message.
- ✅ Decision Session creation is unaffected — `operator_decision_session_service.py` and `trading_decision_service.py` are untouched.
- ✅ TradingAgents advisory contract unchanged; `tradingagents_research_service.py` untouched.

## Observations (non-blocking)

These do not affect correctness or acceptance, but are worth a glance during the next pass:

1. **Untracked files / no commits.** All four new files are still in the working tree as untracked. Per the plan, each task ended with a commit step. The PR-creation step will need to stage and commit them (single squash commit is fine if preferred). No code change required.
2. **Dead `_order(...)` call in `test_two_callers_share_one_pure_service` (test file line 343).** A leftover from rewriting — the result is built but discarded; the next call constructs a `PendingOrderInput` directly. Cosmetic only; does not affect the assertions. Optional cleanup.
3. **Stale-quote check when both `now` and `order.ordered_at` are `None`.** No stale-quote warning is emitted in that case (the only test that exercises stale-quote provides `now=...` explicitly). This matches the plan, but it does mean callers that omit both timestamps silently lose stale-quote protection. Consider documenting the contract in the docstring on `reconcile_pending_order` ("pass `now` to enable stale-quote detection") in a follow-up if it confuses callers.
4. **`bid_ask_spread_pct` decision-support field is computed but no test asserts on it.** The orderbook context passes through to a decision-support field per plan §"decision_support always includes …", but the warnings/classifier never consults it. Coverage hole, not a bug. A small additional test asserting the populated value would lock the contract in. Optional.

None of these are must-fix.

---

## Final verdict

**review_passed.** The implementation matches the plan, all required classifications and warnings are covered, the safety invariant is enforced by an automated subprocess test, and the service is reusable by both downstream callers without touching their modules. The 24 tests pass; lint and format pass. The only outstanding action is committing the four untracked files before opening the PR.

AOE_STATUS: review_passed
AOE_ISSUE: ROB-22
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-22-review-report.md
AOE_NEXT: create_pr
