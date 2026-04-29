# ROB-42 Review Report

**Reviewer:** Opus (planner/reviewer pane)
**Issue:** ROB-42 — Add strategy event timeline and operator event form to Decision Session UI
**Branch:** `feature/ROB-42-strategy-event-ui-timeline`
**Plan:** `docs/plans/ROB-42-strategy-event-ui-timeline-plan.md`
**Implementer:** Sonnet (reported `implementation_done`)
**Result:** **review_passed**

---

## Diff scope

```
git diff --stat main...HEAD  →  15 files changed, 2683 insertions(+), 0 deletions(-)
```

All non-plan changes are confined to `frontend/trading-decision/`. Verified with:

```
git diff --name-only main...HEAD | grep -v '^frontend/trading-decision/' | grep -v '^docs/plans/ROB-42'
→ (empty)
```

No backend, broker, KIS, Upbit, paper, watch, order, or `app/`-tree code was touched.

## Commit hygiene

Per-task TDD commits as planned:

```
080141af  docs(plan): ROB-42 strategy event UI timeline + operator event form
184dfacf  feat(ui): add strategy event TS types for ROB-42                              [Task 1]
a7df9dca  feat(ui): add strategy-events API client (ROB-42)                             [Task 2]
abb296ef  test(ui): add strategy-event fixtures (ROB-42)                                [Task 3]
aecc2d52  feat(ui): add useStrategyEvents hook (ROB-42)                                 [Task 4]
abce467d  feat(ui): add StrategyEventTimeline component (ROB-42)                        [Task 5]
8732957d  feat(ui): add OperatorEventForm component (ROB-42)                            [Task 6]
03df7f67  feat(ui): mount strategy event timeline + operator form in session detail     [Task 7]
acd570f7  test(ui): integration tests for strategy event UI on SessionDetailPage        [Task 8]
```

## Verification — all green

| Check | Command | Result |
|---|---|---|
| Full vitest suite | `npm run test` | **26 files / 113 tests PASS** |
| Typecheck | `npm run typecheck` | PASS (no errors) |
| Production build | `npm run build` | PASS (`dist/index.html` + 328KB JS bundle) |
| Forbidden-mutation safety test | `npm run test -- src/__tests__/forbidden_mutation_imports.test.ts` | PASS |
| Token sweep on new files | `grep -RnE "place_order\|kis_trading_service\|paper_order_handler\|manage_watch_alerts\|fill_notification\|/orders/\|paper_order\|live_order\|watch_alert\|order_intent\|broker"` over the four new sources + the page | no matches |

## Safety guardrail audit

| Guardrail | Verdict | Evidence |
|---|---|---|
| No broker / KIS / Upbit / order / watch / paper / live-execution call | ✅ | New API client (`api/strategyEvents.ts`) hits only `/strategy-events` (read+create). Hook (`hooks/useStrategyEvents.ts`) wraps only those two functions. No other endpoints referenced anywhere in the diff. |
| No order intent / dry-run / watch registration | ✅ | No code path constructs such payloads. Token sweep returned zero matches. |
| No automatic proposal-decision mutation | ✅ | `useStrategyEvents.submit` only POSTs `/strategy-events` and calls a local refetch. Integration test `surfaces a strategy-event submit error without mutating proposals` explicitly asserts `/trading/api/proposals/proposal-btc/respond` is NEVER hit (`proposalRespondCalled = false`). |
| No automatic strategy revision mutation | ✅ | No imports/strings reference revisions. Diff contains zero references to strategy revisions. |
| TradingAgents advisory integration not touched | ✅ | No tradingagents-related strings or imports in the diff. |
| Backend untouched | ✅ | Diff scope check above. |
| forbidden_mutation_imports.test.ts unchanged & passing | ✅ | The test file itself was not modified, and it asserts no source file mentions `place_order`, `kis_trading_service`, `paper_order_handler`, etc. PASS. |

## Required acceptance tests (per ROB-42)

| Spec test | Implementation | File / it() |
|---|---|---|
| 1. Timeline renders session-scoped events | rendered `StrategyEventTimeline` mounted inside `aria-label="Strategy events"` section, fed from `GET /strategy-events?session_uuid=<uuid>` | `SessionDetailPage.test.tsx`: `renders session-scoped strategy events timeline` (line 128); `StrategyEventTimeline.test.tsx`: `renders event type, severity, confidence, symbols, and timestamp` |
| 2. Empty state | `<p>No strategy events yet for this session.</p>` rendered when `events.length === 0` | `SessionDetailPage.test.tsx`: `renders an empty state when there are no strategy events` (line 158); `StrategyEventTimeline.test.tsx`: `renders an empty state when there are no events` |
| 3. Submit POSTs `operator_market_event` with current `session_uuid` | `OperatorEventForm` always builds body with `source: "user"`, `event_type: "operator_market_event"`, `session_uuid: <prop>` | `SessionDetailPage.test.tsx` (line 180): asserts `sentBody.source === "user"`, `event_type === "operator_market_event"`, `session_uuid === "session-1"`, `source_text`, `affected_symbols` |
| 4. Successful submit refreshes/appends timeline | `useStrategyEvents.submit` calls `refetch()` after `createStrategyEvent` resolves; useEffect re-fetches | Same test (line 252): after submit, `findByText(/openai earnings missed/i)` passes — the refetched list contains the new event |
| 5. API error surfaced; no proposal/order mutation | On `ApiError` the form renders `<p role="alert">{detail}</p>`; no proposal mutation triggered | `SessionDetailPage.test.tsx` (line 257): `findByText(/validation failed/i)` passes; `proposalRespondCalled` stays `false` |

All five acceptance tests are present and passing.

## Plan adherence

The implementation follows the plan task-by-task. One small, justified deviation:

- **`OperatorEventForm` adds `noValidate` to `<form>`** (plan did not specify). Necessary because `<input type="number" min={1} max={5}>` would otherwise reject the value `9` typed in the clamp test before `handleSubmit` runs. The component still preserves the typed value via state and clamps it server-side before POST. This is a sensible UX choice that keeps the clamp logic exercisable. Not a must-fix.

Everything else (file paths, function signatures, prop names, hook return shape, CSS classes) matches the plan.

## Code quality observations (non-blocking)

These are observations the next iteration might consider; none block this PR:

1. `OperatorEventForm` resets `severity`/`confidence` back to `"2"` / `"50"` after a successful submit, which silently discards the operator's last setting. If operators tend to log multiple high-severity events in a row, persisting the last values could be friendlier. Defer to ROB-40 follow-ups.
2. The hook's `submit` returns immediately after `refetch()` triggers the new effect tick — the timeline catches up async. The plan explicitly allowed either "refresh OR optimistic append," and refresh is what was implemented. Adequate for this slice.
3. `affected_symbols` is uppercased on the wire only by virtue of operator habit; there's no `.toUpperCase()` normalization in the form. Backend accepts as-is per `_strip_short`. Fine for now; if normalization is desired, address in a follow-up.
4. `useStrategyEvents` does not handle 401 redirect like `useDecisionSession` does — it falls through to the generic `error` state. Consistent with `useSessionAnalytics`. The session route is already gated by the parent `useDecisionSession` hook, which redirects on 401, so by the time this hook runs the user is authenticated. No issue.

None of the above is a must-fix.

## Final verdict

`review_passed` — proceed to PR creation.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-42
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-42-review-report.md
AOE_NEXT: create_pr
