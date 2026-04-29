# ROB-27 — Final Review Signoff (Opus)

- **Branch:** `feature/ROB-27-ui-reconciliation-badges-venue-warnings`
- **Base:** `origin/main`
- **Fix commit:** `0bd1b7c2 fix(rob-27): address reconciliation UI review findings`
- **Original review report:** `docs/plans/ROB-27-review-report.md` (status was `review_must_fix`)
- **Reviewer mode:** Opus, read-only.

## Verdict

**PR-ready.** All four fixes from the original review are present, correct, and verified by re-running the bundle gate locally. No new issues found. No blocking concerns remain.

## Fix-by-fix verification

### 1. Bundle-content gate now distinguishes runtime symbols from English copy

`frontend/trading-decision/src/__tests__/api.decisions.test.ts:175-202` — `forbidden` is now a `RegExp[]` and the loop uses `token.test(body)` against the un-lowercased bundle. Patterns:

```
/kis\./i, /upbit\./i, /redis/i, /telegram/i,
/broker[._]/i,
/order_service/i, /fill_notification/i, /execution_event/i, /watch_alert_service/i
```

The new `/broker[._]/i` is the targeted change: it still catches SDK-shaped occurrences such as `broker.placeOrder(`, `broker_service.`, etc., but no longer matches the user-visible English word "broker" inside copy strings. A short comment was added explaining intent.

Verified locally:

```
cd frontend/trading-decision && npm run build
# → dist/assets/index-BTxbCqTv.js  316.73 kB
RUN_BUNDLE_GREP=1 npm test -- --run api.decisions.test.ts
# → Test Files 1 passed (1) · Tests 9 passed (9)
```

The intent of the safety net is preserved: any future accidental import of `broker_*`, `kis.*`, `upbit.*`, `order_service`, `fill_notification`, `execution_event`, `watch_alert_service`, `redis`, or `telegram` SDK code into the SPA bundle will still trip the gate.

### 2. Generic safety note is hidden when the non-actionable alert is shown

`frontend/trading-decision/src/components/ProposalRow.tsx:155-159` — wrapped in `{!nonActionable ? <p className={styles.safetyNote}>...</p> : null}`. On non-actionable rows the operator now sees only the targeted alert ("Non-NXT pending order — KR broker routing only…"), not both messages. Locked in by a new assertion in `ProposalRow.test.tsx:232-234`:

```tsx
expect(
  screen.queryByText(/Accept records this decision only/i),
).not.toBeInTheDocument();
```

The actionable case still shows the original safety note, as the plan required. The "responding is ledger-only" guarantee is preserved in the non-actionable copy itself ("…recording a response on this row does not place or cancel a broker order").

### 3. `NxtVenueBadge` exposes `aria-label` on every branch

`frontend/trading-decision/src/components/NxtVenueBadge.tsx` — each of the five `<span>` returns now sets `aria-label={`NXT venue: ${badgeLabel}`}`. The label string is hoisted into a local `badgeLabel` const per branch so the visible text and the aria-label cannot drift. The component remains pure presentational, type-safe, and gated on `marketScope === "kr"`.

Locked in by `NxtVenueBadge.test.tsx:36-38`:

```tsx
const badge = screen.getByText("Non-NXT (KR broker)");
expect(badge).toBeInTheDocument();
expect(badge).toHaveAccessibleName("NXT venue: Non-NXT (KR broker)");
```

A single explicit accessible-name assertion is sufficient because the label format is uniform across branches and the same `badgeLabel` variable feeds both `aria-label` and `children` in each branch.

### 4. `ProposalRow` test asserts the duplicate-note removal

`frontend/trading-decision/src/__tests__/ProposalRow.test.tsx:232-234` adds the negative assertion inside the `kr_pending_non_nxt` test, alongside the existing `getByRole("alert")` check. The test file otherwise unchanged.

## Independent re-runs (read-only, in this worktree)

```
cd frontend/trading-decision
npm run build                                         # ✓ 316.73 kB bundle
RUN_BUNDLE_GREP=1 npm test -- --run api.decisions.test.ts
                                                      # ✓ 9/9
npm test -- --run                                     # ✓ 20 files / 84 tests

grep -nrE 'place_order|modify_order|cancel_order|manage_watch_alerts|kis_trading_service|fill_notification|execution_event|paper_order_handler' \
     frontend/trading-decision/src/
# → matches only inside the two safety-test files
#   (forbidden_mutation_imports.test.ts, api.decisions.test.ts), as expected
```

Hermes' broader gate run (recorded in the handoff) is consistent with these results: backend ruff/ty/pytest all green; frontend typecheck/build/test all green.

## Trading-safety re-check

- No new caller of `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, `paper_order_handler`, `kis_trading_service`, `fill_notification`, `execution_event`, or any broker placement / paper / dry-run / live order path. Confirmed by `forbidden_mutation_imports.test.ts` (default test run) and by `RUN_BUNDLE_GREP=1` (built bundle).
- No `dangerouslySetInnerHTML` / `innerHTML`. All untrusted payload values still flow through `parseReconciliationPayload` (allowlists `reconciliation_status`, `nxt_classification`, `candidate_kind`; regex-filters warning tokens) and are rendered as plain text via `formatDecimal` / `formatPercent` / `formatDateTime`.
- Backend wire format unchanged. Sole `app/` diff remains the appended docstring at `app/schemas/research_run_decision_session.py:198+` documenting `original_payload`'s shape — no class, Field, validator, or model change.
- Non-actionable rows are visually distinct (muted background, amber border, role=alert banner) and clearly state that responding is ledger-only. Response controls remain enabled so the operator can record their decision (per plan).

## Residual notes (not blocking)

- The `parseReconciliationPayload` activation heuristic still includes `"candidate_kind"` in `HAS_PAYLOAD_KEYS`. No current code path produces a payload with `candidate_kind` lacking the rest of the reconciliation surface, so this remains informational only — flagged in the original report and not regressed by the fix.
- `NxtVenueBadge.test.tsx` asserts the accessible-name format on one representative branch. Adding the same assertion to the other four branches would harden against future drift, but the consolidated `badgeLabel` const in the component and the uniform aria-label format make the current single assertion adequate.

## Recommendation

Proceed to PR. No must-fix items remain.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-27
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-27-final-review-signoff.md
AOE_NEXT: create_pr
