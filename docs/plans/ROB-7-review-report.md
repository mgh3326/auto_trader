# ROB-7 — Trading Decision Workspace UI · Review Report

- **Reviewer:** Claude Opus (planner/reviewer)
- **Implementer:** Codex YOLO (auto mode)
- **Branch:** `feature/ROB-7-trading-decision-workspace-ui` (14 commits ahead of `origin/main`)
- **Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-7-trading-decision-workspace-ui`
- **Plan reviewed against:** `docs/plans/ROB-7-trading-decision-workspace-ui-plan.md`
- **Verdict:** ✅ **PR-ready** (no blockers; minor non-blocking nits listed in §5)

---

## 1. Verifications run

| Check | Result |
|---|---|
| `cd frontend/trading-decision && npm run typecheck` | PASS (tsc strict, two projects, no diagnostics) |
| `cd frontend/trading-decision && npm run test` | PASS — 9 files, 37 tests, 0 failures, 2.27s |
| `cd frontend/trading-decision && npm run build` | PASS — 49 modules, `dist/index.html` + hashed assets, 412 ms |
| `RUN_BUNDLE_GREP=1 npm run test` | PASS — built bundle has no forbidden runtime tokens |
| `git diff --check origin/main...HEAD` | PASS (no whitespace/conflict markers) |
| `git diff --stat origin/main...HEAD -- 'app/**' 'alembic/**' 'tests/**' 'scripts/**'` | EMPTY (no backend Python edits) |
| `uv run pytest tests/test_trading_decisions_router{,_safety}.py tests/test_trading_decisions_spa_router{,_safety}.py -q` | PASS — 26 tests (ROB-1/2/6 router + safety still green) |
| Source forbidden-token grep over `frontend/trading-decision/src/` (excluding `__tests__/`) for `kis|upbit|telegram|broker|order_service|fill_notification|execution_event|watch_alert|redis` | Only legitimate hits: the literal `"watch_alert"` `ActionKind` string in `api/types.ts` and the `case` arm in `LinkedActionsPanel.tsx`. Both are part of the ROB-2 wire contract — not runtime calls. |
| Bundle URL grep (`/proposals/*/actions`, `/counterfactuals`, `/outcomes`) | Empty — UI does not embed any of those URLs (the only `/actions` substring is inside a react-router internal error string `"/actions must have a Location header"`). |
| `git ls-files frontend/trading-decision/dist` | Empty — `dist/` is correctly ignored. |
| `git ls-files frontend/trading-decision/package-lock.json` | Present (2,278 lines) — lockfile committed. |
| Worktree status | Clean. |

---

## 2. Prompt 4 acceptance verification

| Prompt 4 requirement | Where it lands | Verdict |
|---|---|---|
| List decision sessions | `pages/SessionListPage.tsx` (paginated, status filter, refresh) | ✅ |
| Show detail page with market brief | `pages/SessionDetailPage.tsx` + `components/MarketBriefPanel.tsx` (collapsible `<details>`) | ✅ |
| Detail page shows proposal rows | `components/ProposalRow.tsx` rendered per proposal in `data.proposals` | ✅ |
| accept | `ProposalResponseControls` + `respond({response: "accept"})` | ✅ |
| reject | same | ✅ |
| defer | same | ✅ |
| modify | `ProposalResponseControls` opens `ProposalAdjustmentEditor`; submit POSTs `{response:"modify", user_*}` | ✅ |
| partial accept | same path with `response:"partial_accept"` | ✅ |
| List-style selection (BTC/ETH only, defer SOL) | Each `ProposalRow` is independent; per-row controls; covered by `SessionDetailPage.test.tsx` | ✅ |
| Inline adjustment shows analyst original AND user-adjusted values | `OriginalVsAdjustedSummary` rendered inside `ProposalRow` for non-pending; `ProposalRow.test.tsx` asserts both `20` and `10` are visible after a `modify` | ✅ |
| Linked actions: live order ids, paper ids, watch fields, no-action records | `LinkedActionsPanel` reads from `proposal.actions`; per-`action_kind` external id rendering (`live_order` → `external_order_id`, `paper_order` → `external_paper_id`, `watch_alert` → `external_watch_id`, `no_action`/`manual_note` → `(no external id)`); empty case renders `"No linked actions yet."` | ✅ |
| UI must not call live order execution | `apiFetch` anchored at `/trading/api`; only `GET /decisions`, `GET /decisions/{uuid}`, `POST /proposals/{uuid}/respond` reachable from source | ✅ |
| UI only records decisions/actions via API | UI never POSTs to `/actions`, `/counterfactuals`, or `/outcomes` (verified by source + bundle grep) | ✅ |
| Order/watch registration is a separate flow | UI doesn't create actions; reads them as already-recorded data | ✅ |

All Prompt 4 acceptance lines covered.

---

## 3. Plan §8 acceptance checklist

| Item | Verdict |
|---|---|
| `/trading/decisions/` renders inbox | ✅ (page mounted at `basename: "/trading/decisions"`, route `/`) |
| Click row navigates to `/sessions/{uuid}` | ✅ (`SessionListPage.test.tsx` asserts `href="/sessions/session-1"`) |
| Each row shows analyst original (every non-null `original_*`) | ✅ (`ValueList` walks `valuePairs` and skips `null` entries) |
| Five response controls per row | ✅ |
| `accept`/`defer`/`reject` POST `{response}` (+ optional `user_note`) | ✅ for `{response}` posts; ⚠️ for the optional `user_note` — see §5 nit (1) |
| `modify`/`partial_accept` open inline editor; require ≥1 user_* numeric | ✅ (`ProposalAdjustmentEditor` enforces this client-side; server enforces it again) |
| After modify, `OriginalVsAdjustedSummary` shows both values | ✅ |
| List-style multi-response works (BTC/ETH/SOL independent) | ✅ |
| Linked actions panel renders four kinds; outcomes never rendered | ✅ (test asserts `queryByTestId("outcome-row")` is null after rendering) |
| No backend Python file modified | ✅ (verified by `git diff --stat origin/main...HEAD -- 'app/**' …`) |
| Browser fetches only `/trading/api/*` | ✅ (`apiFetch` constants + bundle grep) |
| 401 → `/login?next=...`; 404 → friendly not-found; 409 → "Session is archived" banner | ✅ |
| `npm run typecheck && npm run test && npm run build` green | ✅ |
| CI runs `npm run test` step | ✅ + adds `RUN_BUNDLE_GREP=1 npm run test` (bonus safety) |
| ROB-2 router tests still pass | ✅ (26 tests green) |
| Diff scope is `frontend/trading-decision/**` + the CI workflow + `docs/plans/ROB-7-*` | ✅ |

---

## 4. Safety boundary verification

The plan's hard constraints (§9 of the plan) all hold:

1. **No live execution.** `apiFetch` is hard-anchored at `/trading/api`. Source has no other base URL. The only out-of-`/trading/api` URL the UI emits is `/login?next=…` for the unauthenticated redirect — exactly as the plan specified.
2. **No secrets.** No `.env` reads in source. No header inspection. The same-origin cookie is sent automatically by the browser.
3. **No outcome rendering.** `LinkedActionsPanel` does not render outcomes; `LinkedActionsPanel.test.tsx` actively asserts `data-testid="outcome-row"` is absent.
4. **Counterfactuals read-only.** No `POST /counterfactuals` in source or bundle.
5. **Actions read-only.** No `POST /actions` in source or bundle.
6. **No backend changes.** `git diff` for `app/**`, `alembic/**`, `tests/**`, `scripts/**` is empty.
7. **No Hermes profile branching.** `source_profile` and `strategy_name` are display-only.
8. **No env reads at runtime.** No `import.meta.env` in non-test source.

The bundle grep test at `api.decisions.test.ts:113` runs in CI under `RUN_BUNDLE_GREP=1` and was clean locally.

---

## 5. Non-blocking nits (worth a follow-up, not a fix-now)

1. **Simple responses (accept/defer/reject) don't allow an optional `user_note`.** The plan §6.5 mentioned "open a small confirm dialog (single line + an optional `user_note`)". The implementation skips the dialog and posts `{response}` only. This is a slight UX regression vs the plan but does not violate any acceptance item — `ProposalRespondRequest.user_note` is optional on the API. Easy to add later as `{response, user_note?}` with a small inline note input.
2. **Currency suffix only attaches to the `Amount` row.** `ProposalRow:130` does `row.label === "Amount" && proposal.original_currency ? \` ${proposal.original_currency}\` : ""`. `Price` and `Trigger price` arguably also deserve the suffix. Cosmetic only.
3. **No tests cover the 401 redirect from inside the hooks.** `api.decisions.test.ts` covers the API-layer 401 (asserts `ApiError(401)` is thrown). The `useDecisionInbox`/`useDecisionSession` `redirectToLogin()` branches are not directly exercised. The branches are short and obviously correct, but a `Object.defineProperty(window, 'location', { … })` test would close the loop.
4. **`SessionListPage.test.tsx:32` asserts `getAllByText("3").toHaveLength(2)`.** Brittle if the fixture ever changes — a `getAllByRole('cell')` pattern would be sturdier. Not failing today.
5. **Dependency versions diverged from the plan's caret ranges.** Plan suggested `vitest@^3`, `jsdom@^26`; Codex installed `vitest@^4.1.5`, `jsdom@^29.1.0`. Both are newer-than-required; tests pass on both. No action.
6. **`MarketBriefPanel` is always closed by default.** The plan said "default closed if non-empty" — implemented as always closed. Fine.
7. **`ProposalAdjustmentEditor` validates `nonNegative` and `percent` ranges client-side**, which slightly over-validates compared to the server (server validates `>= 0` and `0..100` via Pydantic). Behavior matches the API; just noting the duplication. Acceptable defense-in-depth.

None of these block the PR.

---

## 6. Recommendation

**Proceed to PR creation.** All Prompt 4 acceptance criteria, all plan §8 checklist items, and all plan §9 safety constraints are satisfied. Tests are green. Backend is untouched. Diff scope is clean. The bundle has no forbidden runtime tokens. The §5 nits can be follow-up issues.

PR title: `feat(rob-7): trading decision workspace UI`
PR base: `main`
PR body should include the §8 plan checklist and a screenshot pair from `make dev` + `make frontend-dev` (manual smoke is captured in plan Task 13; if not yet captured, encourage the implementer to attach two screenshots before merging).

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-7
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-7-review-report.md
AOE_NEXT: create_pr
