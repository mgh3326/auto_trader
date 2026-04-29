# ROB-27 — Reviewer Report (Opus)

- **Branch:** `feature/ROB-27-ui-reconciliation-badges-venue-warnings`
- **Base:** `origin/main`
- **Scope:** UI reconciliation badges & venue warnings (Research Run + Decision Session SPA)
- **Plan:** `docs/plans/ROB-27-ui-reconciliation-badges-venue-warnings-plan.md`
- **Reviewer mode:** Opus, read-only. No code edits.

## Summary

The implementer delivered the plan as written: 12 frontend files added/modified, 1 backend file changed (comment-only docstring), 7 new test files, 1 modified test file, 1 modified fixture file. The default frontend test suite (`npm test -- --run`) passes (84/84). `npm run typecheck` and `npm run build` are clean.

Trading-safety guardrails are respected:
- No new caller of `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, `paper_order_handler`, `kis_trading_service`, `fill_notification`, broker placement, or any execution-side path.
- No `dangerouslySetInnerHTML` / `innerHTML`. All untrusted payload values are coerced through allowlisted parsers (`pickClassification`, `pickNxtClassification`, `pickWarnings`) or rendered via `formatDecimal`/`formatPercent`/`formatDateTime`.
- New `forbidden_mutation_imports.test.ts` (Task 10) enforces the above against future changes; the only legitimate exception (`api.decisions.test.ts`, which contains the forbidden tokens as test-data string literals for the bundle-content gate) is explicitly skipped with a code comment.
- Backend wire format is unchanged. The single backend diff is a comment block at the bottom of `app/schemas/research_run_decision_session.py` documenting the `original_payload` shape; it does not redefine, override, or constrain any model.

Acceptance criteria coverage:
- Reconciliation badge with green / red / amber states keyed off `reconciliation_status` (`ReconciliationBadge`).
- NXT venue badge with five states keyed off `(market_scope, nxt_classification, nxt_eligible)` (`NxtVenueBadge`).
- Decision-support panel exposing pending side / price / qty, distance to current price, nearest support / resistance, bid/ask spread, live quote, pending order id (`ReconciliationDecisionSupportPanel`).
- Allowlisted warning chips (`WarningChips`) with friendly labels; unknown-shaped tokens dropped, allowlist-shaped unknown tokens passed through as text.
- Non-NXT pending and `data_mismatch_requires_review` rows get a muted background, an amber border, and a `<p role="alert">` reading "Non-NXT pending order — KR broker routing only…" rendered above the response controls. Controls remain enabled so the operator can ledger a decision (per plan).
- `MarketBriefPanel` renders the new structured Research Run summary (research_run_uuid / refreshed_at / counts / reconciliation_summary / nxt_summary / snapshot_warnings / source_warnings) and falls back to `<pre>{JSON.stringify(brief, null, 2)}</pre>` for unstructured briefs (legacy sessions remain unaffected).
- Tests exercise the new states explicitly (near_fill / too_far / kr_pending_non_nxt / data_mismatch_requires_review / actionable not-marked-non-actionable).

## Verification commands run

```
git diff --stat origin/main...HEAD
cd frontend/trading-decision && npm test -- --run        # 20 files, 84 tests, all green
cd frontend/trading-decision && npm run typecheck         # clean
cd frontend/trading-decision && npm run build             # clean (316 kB bundle)
RUN_BUNDLE_GREP=1 cd frontend/trading-decision && npm test -- --run api.decisions.test.ts   # FAILS — see Issue 1
```

(Backend pytest gates and ruff/ty were not re-run because the only backend diff is a commented docstring at end of file. `git diff --name-only origin/main...HEAD -- app/` returns exactly `app/schemas/research_run_decision_session.py`, matching the plan.)

## Findings

### Issue 1 — Bundle-content safety gate fails on the new "broker" copy (must-fix)

**Severity:** must-fix (project safety regression, even though opt-in).

The pre-existing bundle-content guard in `frontend/trading-decision/src/__tests__/api.decisions.test.ts:175-200` runs when `RUN_BUNDLE_GREP=1` and asserts that the built JS bundle does not contain any of:

```
"kis.", "upbit.", "redis", "telegram", "broker",
"order_service", "fill_notification", "execution_event", "watch_alert_service"
```

The intent of the gate is to prevent backend SDK module names (e.g., `broker_*`, `kis_trading_service`, etc.) from being accidentally tree-shaken into the SPA. ROB-27 introduces three legitimate user-visible English strings that contain the substring "broker":

- `frontend/trading-decision/src/components/ReconciliationBadge.tsx:14` — `kr_pending_non_nxt: "KR broker only"`
- `frontend/trading-decision/src/components/NxtVenueBadge.tsx:34` — `Non-NXT (KR broker)`
- `frontend/trading-decision/src/components/ProposalRow.tsx:142-144` — alert text `Non-NXT pending order — KR broker routing only. Review before deciding; recording a response on this row does not place or cancel a broker order.`

After `npm run build`, the bundle `dist/assets/index-*.js` matches the substring `broker` four times, all from the new copy. Reproducer:

```bash
cd frontend/trading-decision
npm run build
RUN_BUNDLE_GREP=1 npm test -- --run api.decisions.test.ts
# AssertionError: expected true to be false
```

This breaks the project's opt-in safety gate, and the gate is what ROB-25/26 leaned on to prove that no broker SDK ships to the browser.

The plan's Task 13 only required the default `npm test -- --run` (which excludes the gated assertion), so the implementer's PR would still pass the plan's quality gates. But the new copy is a real regression of an existing safety net.

**Suggested fix strategy** (to be applied in the fix step, not by this reviewer):
- Refine the bundle-grep token list in `frontend/trading-decision/src/__tests__/api.decisions.test.ts` so the `broker` token only matches SDK-shaped occurrences. For example, replace the substring check with a word-boundary regex that requires a `_` or `.` neighbour: `/broker[._]/i` — this still catches `broker_service`, `broker.placeOrder`, etc., but not the English word "broker" in user-visible copy.
- Apply the same tightening (or remove) to other tokens that could accidentally match user copy in future copy changes (e.g., `redis`, `telegram` — currently low risk but worth revisiting).
- Optionally, add an inline comment in the test explaining the gate's intent so future implementers don't need to re-discover it.
- Do **not** disable the gate or add a `RUN_BUNDLE_GREP=1`-skip hatch; the goal is to keep the safety net active. Do **not** rename the user-visible "broker" copy — the operator needs to understand the routing implication.
- After the fix, re-run `npm run build && RUN_BUNDLE_GREP=1 npm test -- --run api.decisions.test.ts` to confirm green.

### Issue 2 — `safetyNote` and `nonActionableAlert` both render on non-actionable rows (should-fix)

**Severity:** should-fix (UX duplication, not safety).

The plan said: *"Keep the existing safety note (`<p className={styles.safetyNote}>...`) as-is for the actionable case; when nonActionable is true, the new alert above conveys the warning."* The implementation in `frontend/trading-decision/src/components/ProposalRow.tsx:140-157` always renders the existing safety note ("Accept records this decision only; it does not send a live trade.") in addition to the new non-actionable alert. On a non-NXT pending row the operator now sees both:

1. (alert) "Non-NXT pending order — KR broker routing only. Review before deciding; recording a response on this row does not place or cancel a broker order."
2. (note) "Accept records this decision only; it does not send a live trade."

Both messages convey the same "not-an-execution" guarantee. The duplication is not dangerous (if anything, it's belt-and-suspenders for safety), but it diverges from the plan and adds visual noise on the rows that need a clearer single message.

**Suggested fix strategy:** wrap the safety note in `{!nonActionable ? <p ...>...</p> : null}`, or merge the two messages into the one alert when `nonActionable` is true.

### Issue 3 — `NxtVenueBadge` lacks `aria-label` (should-fix, minor)

**Severity:** should-fix (accessibility consistency).

`ReconciliationBadge` exposes an `aria-label` of the form `Reconciliation status: <Label>`. `NxtVenueBadge` does not — its `<span>` has only a class. The badge text itself is screen-reader accessible, so this isn't a blocker, but the inconsistency surfaces in the test file too: `NxtVenueBadge.test.tsx` only uses `getByText(...)`, while the plan noted "tests `aria-label` text". The plan's intent was clearly an accessible label.

**Suggested fix strategy:** add `aria-label={`NXT venue: ${badgeLabel}`}` to each `<span>` branch in `NxtVenueBadge`, then add a single `getByLabelText(...)` assertion to the test file to lock it in.

### Issue 4 — Plan said `NxtVenueBadge` test would cover `non_nxt_pending_ignore_for_nxt`; only `buy_pending_too_far` is covered (nice-to-have)

**Severity:** nice-to-have. The "NXT not actionable" branch in `NxtVenueBadge` is reachable by both `buy_pending_too_far` (when `nxt_eligible !== false`) and `non_nxt_pending_ignore_for_nxt` (when `nxt_eligible !== false`). The test only exercises the first. The combination `non_nxt_pending_ignore_for_nxt` + `nxt_eligible=false` is exercised in `ProposalRow.test.tsx`, so this is already covered indirectly. Worth noting but not blocking.

### Issue 5 — `parseReconciliationPayload` may activate on legacy payloads that happen to contain `candidate_kind` (informational only)

`HAS_PAYLOAD_KEYS` includes `"candidate_kind"`. If any pre-ROB-25 payload happened to ship a `candidate_kind` key without the rest of the reconciliation surface, the parser would activate and render mostly-empty badges (e.g., `unknown` recon status, no NXT badge). I greppped the existing fixtures and code; nothing in the current SPA produces a payload with `candidate_kind` without the rest of the new fields, so this is informational, not actionable. Worth keeping the docstring in `app/schemas/research_run_decision_session.py:198+` accurate so a future change doesn't unintentionally collide.

## Trading-safety check (final)

```
git diff --name-only origin/main...HEAD -- 'app/'
# → app/schemas/research_run_decision_session.py only

git diff origin/main...HEAD -- app/schemas/research_run_decision_session.py
# → only adds a `# ---` comment block at the bottom of the file
#   (no new class, no Field, no validator, no model_dump override)

grep -nrE 'place_order|modify_order|cancel_order|manage_watch_alerts|kis_trading_service|fill_notification|execution_event|paper_order_handler' frontend/trading-decision/src/
# → matches only inside the safety-test files themselves
#   (forbidden_mutation_imports.test.ts and api.decisions.test.ts), as expected
```

`forbidden_mutation_imports.test.ts` passes against the default test invocation. No place where a broker mutation could be called from the frontend was introduced.

## Recommendation

`AOE_STATUS: review_must_fix` — Issue 1 (bundle-content safety gate breakage) is a real regression in an existing safety net. The fix is small (tighten the `broker` token to a word-boundary regex like `/broker[._]/i` in `api.decisions.test.ts`) and does not require touching production code. Issues 2 and 3 should ride along in the same fix commit. Issues 4 and 5 are informational.

After the fix:
- `cd frontend/trading-decision && npm run build && RUN_BUNDLE_GREP=1 npm test -- --run api.decisions.test.ts` should be green.
- `cd frontend/trading-decision && npm test -- --run` should remain at 84+/84+ green (Issue 3's added assertion will bump the count).
- Visual non-actionable row should show one alert message instead of two (Issue 2).

---

AOE_STATUS: review_must_fix
AOE_ISSUE: ROB-27
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-27-review-report.md
AOE_MUST_FIX_COUNT: 1
AOE_NEXT: choose_fix_agent
