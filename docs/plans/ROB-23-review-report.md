# ROB-23 Code Review Report

**Reviewer role:** Claude Opus planner/reviewer (read-only)
**Branch:** `feature/ROB-23-nxt-order-candidate-classifier`
**Initial review date:** 2026-04-28
**Re-review date:** 2026-04-28 (after must-fix implementation)
**Plan reviewed against:** `docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md`
**Linear AC reviewed against:** ROB-23 acceptance criteria + ROB-29 absorption note in the plan

## Final verdict (re-review)

**REVIEW PASSED.** The single must-fix item from the initial review (ROB-29 fail-closed rule + 3 regression tests) has been correctly implemented. All three previously-reproducible regressions now fail-close as required, the new tests pass, and no scope drift was introduced. See "Re-review summary" at the end of this report.

## Initial verdict

**REVIEW MUST FIX — 1 critical safety issue.**

The implementation correctly delivers the bulk of ROB-23: pure module structure, ROB-22 reuse, candidate adapter, holding rules, spread/liquidity warnings, Korean operator summaries, and a safety test that enforces module purity. However, the **ROB-29 fail-closed rule** that the plan explicitly mandates (and that the absorption note pinned as non-negotiable) was not implemented. Three regression tests that the plan named are also missing. Empirical reproduction below shows a KR pending order or candidate with a missing `kr_symbol_universe` row currently default-to-actionable — the exact failure mode the plan forbade.

This is a trading-safety regression, not a stylistic nit. Fix is required before PR.

---

## What was reviewed

| Path | Status | Read |
|---|---|---|
| `app/services/nxt_classifier_service.py` | untracked, 387 lines | yes |
| `tests/services/test_nxt_classifier_service.py` | untracked, 542 lines | yes |
| `tests/services/test_nxt_classifier_service_safety.py` | untracked, 83 lines | yes |
| `docs/plans/ROB-23-nxt-order-candidate-classifier-plan.md` | untracked, plan file | yes (verified ROB-29 rule still present) |

Hermes reported: 63 tests passed (38 NXT + 1 NXT-safety + 23 reconciliation + 1 reconciliation-safety), `ruff check` clean, `ruff format --check` clean, `ty check` clean.

---

## What works (no fixes needed here)

| Acceptance criterion | Status | Evidence |
|---|---|---|
| Builds on ROB-22 reconciliation, does not duplicate it | ✅ | `nxt_classifier_service.py:22-29` imports from `pending_reconciliation_service` only; `_map_recon_to_nxt` consumes a `PendingReconciliationItem` instead of redoing classification logic. |
| Non-NXT KR symbols excluded from NXT-actionable (034220 fixture) | ✅ | `_map_recon_to_nxt` rule on line 151-152; tests `test_pending_non_nxt_kr_maps_to_non_nxt_pending_ignore`, `test_candidate_non_nxt_excluded`, `test_holding_non_nxt_excluded` all use 034220 and assert `nxt_actionable=False`. |
| Pending buy/sell compared against support/resistance + NXT context | ✅ | `_map_recon_to_nxt` lines 164-184 compute `order_to_support_pct` / `order_to_resistance_pct` from `order.ordered_price` and the recon's `nearest_*_price`; tested via `test_buy_pending_at_support_when_order_price_within_near_support_pct`, `test_sell_pending_near_resistance_when_order_price_within_near_resistance_pct`, `test_buy_pending_at_support_in_maintain_band`. |
| Concise operator-facing summary on `NxtClassifierItem.summary` | ✅ | `_build_summary` lines 110-138 implements all 10 templates per plan; 7 summary tests cover each branch including support/resistance interpolation. |
| Tests cover eligible/non-eligible × buy/sell × near/far × candidate × holding | ✅ partial | All combinations present except the missing-KR-universe fail-closed cases (see Critical Issue #1). |
| Spread / liquidity warnings when orderbook present | ✅ | `_apply_orderbook_warnings` lines 187-200; tests `test_wide_spread_warning_emitted_above_threshold`, `test_thin_liquidity_warning_when_threshold_set`, `test_holding_emits_wide_spread_warning_too`. Holding decision-support correctly populates `bid_ask_spread_pct` via `_holding_decision_support`. |
| Pure / read-only / no broker mutation | ✅ | Imports limited to `dataclasses`, `datetime`, `decimal`, `typing`, and `app.services.pending_reconciliation_service`. Safety test `tests/services/test_nxt_classifier_service_safety.py` passes against the forbidden-prefix matrix (broker, KIS, Upbit, brokers, order_service, watch_alerts, paper_trading_service, fill_notification, redis_token_manager, kis_websocket, n8n_pending_*, mcp_server.tooling.order_execution / orders_history / orders_modify_cancel / orders_registration / watch_alerts_registration, app.tasks, app.core.db, redis, httpx, sqlalchemy). |
| No `place_order` / `modify_order` / `cancel_order` / `manage_watch_alerts` / paper / dry-run / live order paths | ✅ | None present; safety test enforces. |
| No DB / Redis side effects | ✅ | None present; safety test enforces. |
| No ROB-20 wiring / API / UI / Prefect integration | ✅ | No router, schema, template, Prefect flow, n8n, or migration changes in scope. |

---

## Critical Issue #1 — ROB-29 fail-closed rule not implemented (MUST FIX)

### What the plan requires

Plan section "ROB-29 absorption note", and Mapping Rule 4 (lines 7-15, 185 of the plan):

> Fail-closed for missing KR universe rows. When `order.market == "kr"` and `KrUniverseContext` is missing (ROB-22 emits warning `missing_kr_universe`), the classifier must **never** return any `*_actionable` / `*_at_support` / `*_near_resistance` label. It returns `data_mismatch_requires_review` with `nxt_actionable=False`, propagates the `missing_kr_universe` warning, and adds reason `missing_kr_universe_fail_closed`. Default-to-actionable is forbidden.

The plan also pinned the helper signature: `_map_recon_to_nxt(recon, *, market, side, order_price, nxt_cfg)` and named three regression tests:

- `test_pending_kr_missing_universe_fails_closed_to_data_mismatch`
- `test_pending_kr_missing_universe_overrides_at_support_attempt`
- `test_candidate_kr_missing_universe_fails_closed`

### What the implementation actually does

`app/services/nxt_classifier_service.py:141-184`:

- `_map_recon_to_nxt` signature is `(recon, *, side, order_price, nxt_cfg)` — the `market` keyword is missing.
- The function has no check for `"missing_kr_universe" in recon.warnings`.
- A `near_fill` / `maintain` reconciliation result with `kr_universe=None` falls through to the S/R-proximity branch and returns `*_actionable` (or even `*_at_support` / `*_near_resistance` when SR data is present).
- The string `missing_kr_universe_fail_closed` does not appear anywhere in `app/services/` or `tests/services/`.

The three regression tests named in the plan are absent from `tests/services/test_nxt_classifier_service.py`.

### Empirical reproduction (run from this worktree)

```
$ uv run python -c "
from decimal import Decimal
from app.services.nxt_classifier_service import classify_nxt_pending_order
from app.services.pending_reconciliation_service import (
    MarketContextInput, PendingOrderInput, QuoteContext,
    SupportResistanceContext, SupportResistanceLevel,
)
order = PendingOrderInput(order_id='X', symbol='999999', market='kr', side='buy',
    ordered_price=Decimal('70000'), ordered_qty=Decimal('10'),
    remaining_qty=Decimal('10'), currency='KRW', ordered_at=None)
ctx = MarketContextInput(
    quote=QuoteContext(price=Decimal('70200'), as_of=None), orderbook=None,
    support_resistance=SupportResistanceContext(
        nearest_support=SupportResistanceLevel(price=Decimal('70300'), distance_pct=Decimal('0.5')),
        nearest_resistance=None),
    kr_universe=None)
item = classify_nxt_pending_order(order, ctx)
print(item.classification, item.nxt_actionable, item.summary)
"
buy_pending_at_support True NXT 매수 대기 — 지지선 근접 (지지선 70300)
```

A KR pending order with **no** `kr_symbol_universe` row but a nearby support level is currently classified `buy_pending_at_support` with `nxt_actionable=True` and an operator-facing summary that actively recommends the line. Per the ROB-29 absorption note this scenario must instead return `data_mismatch_requires_review` with `nxt_actionable=False`.

Two further regressions reproduce identically:

| Scenario | Current output | Required output |
|---|---|---|
| KR pending buy, quote present, SR missing, `kr_universe=None` | `buy_pending_actionable`, `nxt_actionable=True` | `data_mismatch_requires_review`, `nxt_actionable=False`, reason `missing_kr_universe_fail_closed` |
| KR pending buy, quote present, SR triggers at-support, `kr_universe=None` | `buy_pending_at_support`, `nxt_actionable=True` (worse — promoted) | `data_mismatch_requires_review`, `nxt_actionable=False` |
| Candidate buy, quote present, SR missing, `kr_universe=None` | `buy_pending_actionable`, `nxt_actionable=True` | `data_mismatch_requires_review`, `nxt_actionable=False`, reason `missing_kr_universe_fail_closed` |

### Why this matters for trading safety

The whole point of ROB-29 absorption was that the broker's KIS pending list (`inquire-psbl-rvsecncl`, `TTTC8036R`) returns KR pending orders for symbols that may or may not be NXT-eligible. If `kr_symbol_universe` is empty, stale, or the row is simply missing for a given symbol, the classifier currently *invents* an actionable NXT signal where the data does not support one. Downstream ROB-25 wiring (or any consumer that filters on `nxt_actionable`) would treat these as live NXT proposals — the precise failure ROB-29 was created to prevent.

This is a fail-open default in a safety-critical decision-support code path. It is a trading-safety regression, not a stylistic concern.

### Must-fix instructions

#### Fix 1 — Update `_map_recon_to_nxt` signature and add rule 4

In `app/services/nxt_classifier_service.py`, change the function signature and insert the new rule between the existing `kr_pending_non_nxt` check and the `unknown` check. Apply this edit:

```python
def _map_recon_to_nxt(  # noqa: C901 (rule-by-rule classifier)
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
    # ... rest unchanged ...
```

#### Fix 2 — Update the two call sites to pass `market`

In `classify_nxt_pending_order` (around line 249) replace:

```python
classification, extra_reasons = _map_recon_to_nxt(
    recon, side=order.side, order_price=order.ordered_price, nxt_cfg=nxt_cfg
)
```

with:

```python
classification, extra_reasons = _map_recon_to_nxt(
    recon,
    market=order.market,
    side=order.side,
    order_price=order.ordered_price,
    nxt_cfg=nxt_cfg,
)
```

In `classify_nxt_candidate` (around line 298) replace:

```python
classification, extra_reasons = _map_recon_to_nxt(
    recon,
    side=candidate.side,
    order_price=candidate.proposed_price,
    nxt_cfg=nxt_cfg,
)
```

with:

```python
classification, extra_reasons = _map_recon_to_nxt(
    recon,
    market="kr",
    side=candidate.side,
    order_price=candidate.proposed_price,
    nxt_cfg=nxt_cfg,
)
```

(NXT classification is a KR-only concept, and the candidate adapter already hard-codes `market="kr"` on its proxy `PendingOrderInput`, so passing `market="kr"` here is correct.)

#### Fix 3 — Add the three regression tests

Append to `tests/services/test_nxt_classifier_service.py` (after the existing pending tests; placement does not matter, but keep them grouped):

```python
@pytest.mark.unit
def test_pending_kr_missing_universe_fails_closed_to_data_mismatch() -> None:
    """ROB-29 fail-closed: KR pending without a kr_symbol_universe row must
    NEVER fall through to *_actionable / *_at_support. Default-to-actionable
    is a safety regression."""
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
```

#### Fix 4 — Run verification before declaring done

```bash
uv run pytest tests/services/test_nxt_classifier_service.py::test_pending_kr_missing_universe_fails_closed_to_data_mismatch tests/services/test_nxt_classifier_service.py::test_pending_kr_missing_universe_overrides_at_support_attempt tests/services/test_nxt_classifier_service.py::test_candidate_kr_missing_universe_fails_closed -v
uv run pytest tests/services/test_nxt_classifier_service.py tests/services/test_nxt_classifier_service_safety.py tests/services/test_pending_reconciliation_service.py tests/services/test_pending_reconciliation_service_safety.py -q
uv run ruff check app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
uv run ruff format --check app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
uv run ty check app/services/nxt_classifier_service.py tests/services/test_nxt_classifier_service.py
```

All five must pass. The three new tests should be in the passing count; the regression demonstration above must no longer reproduce.

---

## Non-blocking observations (do not require fix)

These are notes for the implementer; **do not** treat them as additional must-fix items.

1. **Test names do not include `034220` for the non-NXT cases.** The plan named the tests `test_pending_non_nxt_kr_034220_maps_to_non_nxt_pending_ignore` / `test_candidate_non_nxt_034220_excluded`; the implementation uses `test_pending_non_nxt_kr_maps_to_non_nxt_pending_ignore` / `test_candidate_non_nxt_excluded`. The 034220 fixture is still asserted in both — the symbol `"034220"` and `nxt_eligible=False` appear in the test bodies — so the AC is met. Leaving as-is is acceptable; renaming is optional.
2. **All three implementation files are still untracked.** The plan called for per-task commits ending in a `git push` and `gh pr create`. Hermes will need to stage and commit these before opening the PR. Not a correctness issue.
3. **Holding `data_mismatch` short-circuit skips `_apply_orderbook_warnings`** (`nxt_classifier_service.py:337-349`). When a holding has both `non_positive_quantity` and a wide-spread orderbook, the wide-spread warning is dropped. The plan does not require accumulating both signals on holdings, and `data_mismatch_requires_review` is the dominant operator instruction here, so the early exit is acceptable. Optional: move the orderbook-warning call before the early return if you want full warning fidelity.
4. **Implementation summary helper carries `# noqa: C901`** but is well under the cognitive-complexity threshold. The suppression is harmless. Leave it.
5. **`_holding_decision_support` recomputes spread independently from `_apply_orderbook_warnings`.** This is correct — holdings do not go through reconciliation so they need their own decision-support shape. No DRY violation; the two helpers serve different inputs.

None of the above blocks the PR.

---

## Summary

- 1 critical safety must-fix (ROB-29 fail-closed rule + 3 regression tests).
- 0 stylistic must-fixes.
- All other ROB-23 acceptance criteria satisfied.
- ROB-22 reuse, purity, and out-of-scope discipline are clean.

After Fix 1–4 land and verification commands re-pass, this is mergeable.

AOE_STATUS: review_must_fix
AOE_ISSUE: ROB-23
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-23-review-report.md
AOE_MUST_FIX_COUNT: 1
AOE_NEXT: start_fix_implementer

---

## Re-review summary (2026-04-28, post-fix)

### Verification of each must-fix item

| Item | Required | Implemented | Result |
|---|---|---|---|
| `_map_recon_to_nxt` accepts `market` keyword | Yes | `app/services/nxt_classifier_service.py:144` adds `market: str` after the `*` marker, keeping the rest of the contract unchanged. | ✅ |
| Fail-closed rule positioned after `kr_pending_non_nxt`, before quote/actionable mapping | Yes | `nxt_classifier_service.py:154-158` — the new check sits between the `kr_pending_non_nxt` short-circuit (`:152-153`) and the `unknown` / `too_far` / `chasing_risk` / S-R proximity checks (`:159+`). | ✅ |
| Reason `missing_kr_universe_fail_closed` appended | Yes | `nxt_classifier_service.py:157`. | ✅ |
| Returns `data_mismatch_requires_review` (so `_is_nxt_actionable` returns False) | Yes | `nxt_classifier_service.py:158`. `data_mismatch_requires_review` is not in `_NXT_ACTIONABLE_LABELS` (`:86-93`), so `nxt_actionable=False` is enforced at the wrapper layer too. | ✅ |
| `classify_nxt_pending_order` passes `market=order.market` | Yes | `nxt_classifier_service.py:255-261`. Uses the order's actual market (so a future US/crypto caller would not erroneously trip the KR rule). | ✅ |
| `classify_nxt_candidate` passes `market="kr"` | Yes | `nxt_classifier_service.py:308-314`. Matches the candidate adapter's hard-coded `market="kr"` on the proxy `PendingOrderInput` (`:297`). | ✅ |
| Test `test_pending_kr_missing_universe_fails_closed_to_data_mismatch` present and passing | Yes | `tests/services/test_nxt_classifier_service.py:548-562`. Asserts classification, `nxt_actionable=False`, both `missing_kr_universe` warning and `missing_kr_universe_fail_closed` reason. | ✅ |
| Test `test_pending_kr_missing_universe_overrides_at_support_attempt` present and passing | Yes | `tests/services/test_nxt_classifier_service.py:566-583`. Constructs the would-be at-support fixture and asserts override. | ✅ |
| Test `test_candidate_kr_missing_universe_fails_closed` present and passing | Yes | `tests/services/test_nxt_classifier_service.py:587-598`. Candidate parity. | ✅ |

### Empirical regression check

The three scenarios that previously default-to-actionable were re-run from this worktree:

```
S1 pending, no SR, no kr_universe : data_mismatch_requires_review | nxt_actionable=False | reason missing_kr_universe_fail_closed=True
S2 pending + SR + no kr_universe   : data_mismatch_requires_review | nxt_actionable=False | reason missing_kr_universe_fail_closed=True
S3 candidate, no kr_universe       : data_mismatch_requires_review | nxt_actionable=False | reason missing_kr_universe_fail_closed=True
```

All three fail-close as the plan requires. The previous fail-open default is gone.

### Test suite

```
$ uv run pytest tests/services/test_nxt_classifier_service.py tests/services/test_nxt_classifier_service_safety.py tests/services/test_pending_reconciliation_service.py tests/services/test_pending_reconciliation_service_safety.py -q
66 passed, 2 warnings in 1.35s
```

Counts reconcile against expected: 38 prior NXT tests + 3 new ROB-29 regressions = 41 NXT classifier tests; plus 1 NXT safety + 23 reconciliation + 1 reconciliation safety = 66 total. Matches the Hermes report.

The two Pydantic warnings originate from `app/auth/schemas.py` (pre-existing, unrelated to ROB-23).

Hermes also confirmed: `ruff check`, `ruff format --check`, and `ty check` all pass against the ROB-23 files.

### Scope and safety re-check

Searched the implementation for any forbidden references introduced during the fix:

- `place_order` / `modify_order` / `cancel_order` / `manage_watch_alerts` / `paper_order` / `dry_run` / `kis_websocket` / `httpx` / `sqlalchemy` / `redis` — only one match: a docstring sentence on line 3 of `nxt_classifier_service.py` listing the modules the file must not import. No actual imports or call sites added.
- `prefect` / `router` — none.
- `tests/services/test_nxt_classifier_service_safety.py` — unchanged contract, still passes against the same forbidden-prefix matrix.

The fix touched only the classifier helper signature, one mapper rule, the two call sites that pass `market`, and three appended tests. No new mutation paths, no broker / DB / Redis surface, no API / UI / Prefect wiring. Module purity preserved.

### Non-blocking observations from initial review

These remain as in the initial section above and still do not require action:

1. Test names do not include `034220` literal in their test IDs (the symbol is asserted in test bodies). Optional rename.
2. Files are still untracked — Hermes will commit and push when creating the PR per the plan's Task 11.
3. Holding `data_mismatch` short-circuit skips `_apply_orderbook_warnings`. Acceptable; data_mismatch is the dominant signal.

### Final result

All initial-review must-fix items resolved. No new must-fix items found. Implementation is mergeable.

AOE_STATUS: review_passed
AOE_ISSUE: ROB-23
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-23-review-report.md
AOE_NEXT: create_pr
