# ROB-12 Review Report — Trading Decision Outcome Analytics UI

- **Branch:** `feature/ROB-12-trading-decision-outcome-analytics-ui`
- **PR:** https://github.com/mgh3326/auto_trader/pull/602
- **Linear:** https://linear.app/mgh3326/issue/ROB-12
- **Plan:** `docs/plans/ROB-12-trading-decision-outcome-analytics-ui-plan.md`
- **Reviewer:** Claude Opus (read-only)
- **Verdict:** ✅ **APPROVE — review passed.** No must-fix findings. A small set of optional follow-ups noted under §6.

---

## 1. Diff scope

`git diff origin/main...HEAD --stat` shows **28 files, +3012 / −10**. The plan documentation (1677 lines) accounts for the bulk; the production diff is ~1335 lines, dominated by the new SPA components and their tests.

| Layer | Files modified | Files created |
|---|---|---|
| Backend | `app/routers/trading_decisions.py`, `app/services/trading_decision_service.py`, `app/schemas/trading_decisions.py` | – |
| Backend tests | `tests/test_trading_decisions_router.py`, `tests/models/test_trading_decision_service.py` | – |
| Frontend lib | `frontend/trading-decision/src/api/types.ts`, `…/api/decisions.ts`, `…/hooks/useDecisionSession.ts`, `…/test/fixtures.ts` | `…/hooks/useSessionAnalytics.ts` |
| Frontend UI | `…/components/ProposalRow.tsx`, `…/pages/SessionDetailPage.tsx`, `…/components/ProposalRow.module.css`, `…/pages/SessionDetailPage.module.css` | `…/components/OutcomesPanel.tsx`+css, `…/components/OutcomeMarkForm.tsx`+css, `…/components/AnalyticsMatrix.tsx`+css |
| Frontend tests | `…/__tests__/ProposalRow.test.tsx`, `…/__tests__/SessionDetailPage.test.tsx`, `…/__tests__/api.decisions.test.ts` | `…/__tests__/OutcomesPanel.test.tsx`, `…/__tests__/OutcomeMarkForm.test.tsx`, `…/__tests__/AnalyticsMatrix.test.tsx`, `…/__tests__/useDecisionSession.test.tsx` |

---

## 2. Trading-safety constraints — all green

- **Forbidden imports**: grep across `app/routers/trading_decisions.py`, `app/routers/trading_decisions_spa.py`, `app/services/trading_decision_service.py`, `app/schemas/trading_decisions.py` for `app.services.kis*`, `app.services.upbit*`, `app.services.brokers`, `app.services.order_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.kis_websocket*`, `app.services.redis_token_manager`, `app.tasks` returns **0 hits**.
- **Router safety test** re-run locally: `uv run pytest tests/test_trading_decisions_router_safety.py -v` → 1 passed (transitive imports clean).
- **No DB schema / migration changes**: `git diff origin/main...HEAD --name-only | grep -E "alembic|migration"` → empty. The new code reuses the existing `TradingDecisionOutcome` model and its unique index unchanged.
- **No dependency changes**: `git diff origin/main...HEAD -- frontend/trading-decision/package.json frontend/trading-decision/package-lock.json` → 0 lines. Same for `pyproject.toml`/`uv.lock`.
- **No live broker / order execution paths**: the new analytics endpoint is a pure read; the form posts to the pre-existing outcome-write endpoint that ROB-2 already shipped; no order/watch enrollment introduced.
- **No Hermes profile routing**: nothing under `app/services/` or routers references `source_profile` for routing — confirmed.
- **No new secrets handling**: no env-var reads, no SSM/secret manager calls, no token files.

---

## 3. Plan adherence

The implementer executed every task in the planned order (B1 → B2 → B3 → F1 → F2 → F3 → F4 → F5 → F6 → F7 → F8). Twelve commits, each scoped to one task with the conventional `feat(rob-12):` / `test(rob-12):` / `docs(rob-12):` prefixes:

```
0b00b456 docs(rob-12): plan trading decision outcome analytics UI
eddd83ef feat(rob-12): aggregate session outcomes by track/horizon            ← B1
e8fce369 feat(rob-12): session analytics response schema                      ← B2
d71ef79b feat(rob-12): GET session analytics endpoint                         ← B3
e6fdc0b4 feat(rob-12): analytics + outcome create api client                  ← F1
8f778721 test(rob-12): outcome and analytics fixtures                         ← F2
40f90e13 feat(rob-12): outcome marks panel                                    ← F3
fb6ba370 feat(rob-12): outcome mark creation form                             ← F4
74104c9a feat(rob-12): render outcome marks and form per proposal             ← F5
549845c2 feat(rob-12): useSessionAnalytics hook                               ← F6
26ed5a20 feat(rob-12): analytics matrix component                             ← F7
12e56a6a feat(rob-12): mount analytics matrix on session detail page         ← F8
```

Code matches the plan’s reference snippets to the line, with two non-substantive deviations:
- `useDecisionSession` test was placed in a new `useDecisionSession.test.tsx` file rather than extended in-place. Equally valid, slightly cleaner.
- F8 added `loading` / `error` state UI for the analytics section, beyond the plan’s minimal “success-only” suggestion. Improvement, not a regression.

---

## 4. Backend correctness

### Service layer (`aggregate_session_outcomes`)

- Ownership check first (`SELECT id FROM trading_decision_sessions WHERE session_uuid=… AND user_id=…`), returning `None` on miss → router maps to 404. Matches existing pattern (no 403/404 leak distinction).
- Aggregation joins `trading_decision_outcomes → trading_decision_proposals` filtered by `session_id`, `GROUP BY (track_kind, horizon)`, with `count`, `count(distinct proposal_id)`, `avg(pnl_pct)`, `sum(pnl_amount)`, `max(marked_at)`. SQL is straightforward and uses parameterised SQLAlchemy expressions — no injection risk.
- Frozen `@dataclass` `AggregatedOutcomeCell` keeps the service decoupled from Pydantic, mirroring the existing service style.
- Decimal arithmetic stays in `Decimal` end-to-end; no float conversion. PostgreSQL `AVG(numeric)` and `SUM(numeric)` preserve the `Numeric` type.

### Schemas (`SessionAnalyticsCell`, `SessionAnalyticsResponse`)

- Track and horizon fields use the existing `TrackKindLiteral` / `OutcomeHorizonLiteral` types — ensures the response is locked to the same five tracks and six horizons as the model (and is rejected by Pydantic if drifted).
- `mean_pnl_pct`, `sum_pnl_amount`, `latest_marked_at` are nullable `Decimal | None` / `datetime | None` — correctly modelling cells where some marks didn't ship pnl values.
- The new `test_session_analytics_response_serializes_decimal_strings` confirms `model_dump(mode="json")` emits decimals as strings (`"1.5"`, `"12.34"`), preserving the SPA’s `DecimalString` contract.

### Router (`GET /api/decisions/{session_uuid}/analytics`)

- Auth dep is the same `get_authenticated_user` used by every other route here — cookie-session, no new auth surface.
- 404 on `cells is None` returns the same `"Session not found"` body shape the SPA already handles.
- The static `tracks` / `horizons` arrays match the plan and the DB CHECK constraints exactly.
- Two unit tests cover happy path and 404; existing safety + schema tests still green.

### Test coverage delta

- 2 new integration tests in `test_trading_decision_service.py` (cells aggregation + cross-user 404).
- 2 new unit tests in `test_trading_decisions_router.py` (analytics happy path + 404).
- 1 new schema serialization test.
- Hermes-reported counts: `tests/test_trading_decisions_router.py` 21 passed (was 17), `tests/models/test_trading_decision_service.py` 1 passed / 10 skipped — the 10 skips are the pre-existing integration tests gated on the test DB, not regressions.

---

## 5. Frontend correctness

### API client + types

- `getSessionAnalytics(sessionUuid)` and `createOutcomeMark(proposalUuid, body)` mirror the existing `getSession` / `respondToProposal` style precisely (cookie auth via `apiFetch`, URL-encoded path params, JSON body). `OutcomeCreateRequest` and `SessionAnalyticsResponse` types match the backend exactly, with all decimal fields typed as `DecimalString` (no number widening).
- New `api.decisions.test.ts` cases assert: GET to `/decisions/:uuid/analytics`, POST to `/proposals/:uuid/outcomes` with the JSON body, and that the response shape is wired through correctly.

### Hooks

- `useSessionAnalytics(sessionUuid)` — copies the loading/error/not-found state machine from `useDecisionSession`. Aborts the fetch on unmount via `AbortController`. Re-runs on `sessionUuid` change.
- `useDecisionSession.recordOutcome(proposalUuid, body)` — symmetric with the existing `respond()`: try POST → on success refetch, on `ApiError(401)` redirect to login, on other `ApiError` surface `{ok:false, status, detail}`. The `useDecisionSession.test.tsx` verifies the refetch (two GET calls observed after a successful POST).

### Components

- **OutcomesPanel** — pure presentational table, rows = 5 tracks, cols = 6 horizons. Cells render `pnl_pct` formatted to two decimals + `%`, with a hover `title` containing `price_at_mark`, `pnl_amount`, and `marked_at`. Empty state when `outcomes` is empty. Accessible: `aria-label="Outcome marks"`, `<th scope="row|col">`. Tests cover empty + populated cases.
- **OutcomeMarkForm** — client-side validation mirrors the server invariants (price must be a non-negative finite number; `accepted_live ↔ counterfactual_id` exclusivity). On non-`accepted_live` track, the form filters counterfactuals by `c.track_kind === trackKind` so the user can only attach a CF that matches the selected track. On submit error, the parent’s `{ok:false, detail}` is displayed via `role="alert"`. Numeric inputs reset on success, but `track`/`horizon` persist for fast multi-mark entry. Three tests cover happy-path submit, blocked submit, and conditional CF dropdown.
- **AnalyticsMatrix** — same table shape, sourced from the server-rendered analytics payload. Uses a `Map<string, Cell>` lookup (`trackKind|horizon`) instead of an `Array.find` per cell, so render is O(tracks × horizons + cells). Empty state when `cells.length === 0`. Tests cover empty + populated.

### Wiring

- `ProposalRow` now accepts `onRecordOutcome` and renders `OutcomesPanel` + `<details><summary>Record outcome mark</summary><OutcomeMarkForm …/></details>`. The collapsed `<details>` keeps the existing UI compact. Existing tests were updated to pass the new prop, plus two new tests confirm the panel renders and the form propagates the body to `onRecordOutcome` with the correct proposal UUID.
- `SessionDetailPage` mounts `useSessionAnalytics` alongside `useDecisionSession` and renders an `<AnalyticsMatrix>` section under the market brief panel. Loading / error states show small inline messages; not-found is silently skipped (correct — a session with zero outcomes is normal). The page test now mocks both endpoints and asserts the matrix renders the seeded `1.25%` cell.

### CSS

- All styling uses CSS modules per existing pattern; no global selectors, no new design tokens beyond CSS custom-property references already present (`--border`, `--muted`, `--danger`).

---

## 6. Observations / non-blocking follow-ups

These are quality-of-life ideas; none block this PR.

1. **Analytics staleness after mark recording.** `recordOutcome` triggers `useDecisionSession.refetch()` (so `OutcomesPanel` updates), but `useSessionAnalytics` has no `refetch` exposed, so the `AnalyticsMatrix` does not update until the user navigates away and back. Consider exposing `refetch` from `useSessionAnalytics` and calling it inside `recordOutcome`'s success branch. Minor; the matrix becomes consistent on the next navigation.
2. **Missing 401 handling in `useSessionAnalytics`.** Unlike `useDecisionSession`, the analytics hook does not call `redirectToLogin()` on `ApiError(401)` — it surfaces the error to the user. If a session expires while the page is open, the analytics block shows an error instead of redirecting. Easy parity fix.
3. **Form `marked_at` is hard-coded to `Date.now()`.** Useful for live marks but prevents back-marking historical horizons. If the workflow ever needs back-marking, expose an optional `marked_at` field; for now the implicit-now behaviour matches the most common use case.
4. **`OutcomesPanel` shows the FIRST matching mark per (track, horizon)** via `outcomes.find(...)`. This is correct because the DB unique index `(proposal_id, counterfactual_id, track_kind, horizon)` enforces at most one mark per cell — but if an analyst ever creates two counterfactuals of the same `track_kind` on a single proposal, the panel would only show one. The data model permits this (no uniqueness on counterfactuals), so a future refinement might key the panel by counterfactual_id when present.
5. **`useDecisionSession.test.tsx` does not unstub `fetch` between tests.** Only the final test calls `vi.unstubAllGlobals()`. With only one test in this file today it’s fine, but a per-test `afterEach` would prevent surprises when more tests are added.

None of the above are required for ship.

---

## 7. Validation matrix (all reproduced or accepted)

| Check | Source | Result |
|---|---|---|
| `git diff --check` | Hermes pre-flight | clean |
| `uv run pytest tests/test_trading_decisions_router.py -v` | Hermes | 21 passed |
| `uv run pytest tests/models/test_trading_decision_service.py -v` | Hermes | 1 passed, 10 skipped (pre-existing integration skips) |
| `uv run pytest tests/test_trading_decisions_router_safety.py -v` | Reviewer (re-run) | **1 passed** ✅ |
| `uv run pytest tests/test_trading_decisions_spa_router.py tests/test_trading_decisions_spa_router_safety.py -v` | Hermes | 7 passed |
| `cd frontend/trading-decision && npm run typecheck` | Hermes | passed |
| `cd frontend/trading-decision && npm test` | Hermes | 13 files / 49 tests passed |
| `cd frontend/trading-decision && npm run build` | Hermes | passed |
| `package.json` unchanged | Hermes + reviewer (`git diff` 0 lines) | confirmed |
| Forbidden-import grep (4 trading-decision files) | Reviewer | 0 hits |
| Schema/migration grep | Reviewer | 0 hits |

---

## 8. Recommendation

**APPROVE for merge.** The PR delivers everything Prompt 5 asked for in a strictly additive way, preserves the trading-safety perimeter, and ships solid TDD-backed tests at every layer. Nothing about the change touches live brokers, secrets, dependencies, schema, or Hermes profile routing.

Hermes may proceed with CI / merge / deploy / smoke as planned.
