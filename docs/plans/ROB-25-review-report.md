# ROB-25 — Review report (final)

- Implementation commits reviewed: `ca2be013` (initial), `46d6caab` (8-MF fix bundle), `495819d1` (eager-loaded proposals follow-up).
- Plan: `docs/plans/ROB-25-research-run-decision-session-plan.md`
- Linear: ROB-25 (parent ROB-21)
- Worktree / branch: `feature/ROB-25-research-run-decision-session`
- **Review verdict: PASSED — PR-ready.**

---

## 1. Status of prior must-fix items

All 8 must-fixes from the prior review (commit `43de501b` report) are resolved. Each fix re-verified by file inspection in this worktree.

| ID | Issue | Resolution | Verified |
|---|---|---|---|
| MF-1 | Router `result.research_run_uuid` AttributeError | `app/routers/research_run_decision_sessions.py:101` now uses `result.research_run.run_uuid` | ✅ |
| MF-2 | `cash_balances` / `holdings_by_symbol` silently stubbed | `cash_unavailable` and `holdings_unavailable` warning tokens emitted into `LiveRefreshSnapshot.warnings` (provider lines 205-206) and propagated to `market_brief.snapshot_warnings`; v1 limitation now signalled | ✅ |
| MF-3 | ROB-29 fail-closed bypassed in live-refresh path | Provider now queries `KRSymbolUniverse` directly (lines 123-152). Missing rows are **omitted** from `kr_universe_by_symbol` and emit `missing_kr_universe:{symbol}` warning. Classifier downstream correctly sees `kr_universe=None` → `data_mismatch_requires_review` | ✅ |
| MF-4 | Orchestrator integration tests errored with `relation "users" does not exist`; candidate factory didn't persist | `tests/conftest.py` `db_session` fixture now runs `Base.metadata.create_all` and creates schemas (`paper`, `research`, `review`); `research_run_candidate_factory` now accepts `db_session` and `db_session.add(...)` + `await db_session.flush()` | ✅ |
| MF-5 | Service safety test false-positive prone (no `check=True`, no PYTHONPATH) | `tests/test_research_run_decision_session_service_safety.py` now uses `check=True`, `cwd=project_root`, `env["PYTHONPATH"]=project_root`, **and** sanity-asserts `len(loaded) > 100` | ✅ |
| MF-6 | Duplicate safety test under `tests/services/...` | File deleted (`tests/services/test_research_run_decision_session_service_safety.py` — no longer present) | ✅ |
| MF-7 | `ProposalPayload` `extra="forbid"` rejected `reconciliation_summary` / `nxt_summary` | Schema now declares both as `str \| None = None` (`app/schemas/research_run_decision_session.py:170,172`) | ✅ |
| MF-8 | Out-of-scope 1585-line alternate plan committed | `docs/superpowers/plans/2025-01-24-research-run-decision-session.md` removed | ✅ |

### Follow-up commit `495819d1` (Hermes)

After 46d6caab, the orchestrator service still hit `MissingGreenlet` lazy-load failures because (a) `research_run.candidates` / `research_run.reconciliations` were lazy-loaded outside an active greenlet context after fixture teardown, and (b) the returned `session` exposed `proposals` lazily. The follow-up:

- Adds explicit eager loaders `_load_research_run_candidates` and `_reconciliations_by_order_id` that issue their own `select()` queries instead of touching ORM-attached collections.
- Reloads the persisted session with `selectinload(TradingDecisionSession.proposals)` before returning, so `result.session.proposals` is materialized.
- Pins `source_profile="research_run"` on the persisted session (was previously copied from `research_run.source_profile` — the plan §4.2 specified the literal `"research_run"`).

This fix is well-scoped, async-correct, and aligns the persisted session with the plan and with the existing assertion `result.session.source_profile == "research_run"`. No safety regressions.

---

## 2. Independent test re-run

Reproduced the implementer's invocations in this worktree:

```
uv run pytest \
  tests/test_research_run_decision_session_service.py \
  tests/test_research_run_decision_session_router.py \
  tests/test_research_run_live_refresh_service.py \
  tests/test_research_run_decision_session_service_safety.py \
  tests/test_research_run_decision_session_router_safety.py -q
→ 23 passed, 19 warnings in 4.59s
```

```
uv run pytest \
  tests/test_research_run_schemas.py \
  tests/test_research_run_decision_session_schemas.py \
  tests/test_research_run_decision_session_service.py \
  tests/test_research_run_decision_session_service_safety.py \
  tests/test_research_run_live_refresh_service.py \
  tests/test_research_run_decision_session_router.py \
  tests/test_research_run_decision_session_router_safety.py \
  tests/test_trading_decisions_router.py \
  tests/test_trading_decisions_router_safety.py \
  tests/test_trading_decisions_spa_router.py \
  tests/test_trading_decisions_spa_router_safety.py \
  tests/test_trading_decision_session_url.py \
  tests/test_operator_decision_session_schemas.py -q
→ 84 passed, 66 warnings in 7.40s
```

No errors, no failures. Existing operator / SPA / router safety / synth flows continue to pass — plan §6.6 acceptance criterion (existing flows still pass) verified.

---

## 3. Trading-safety guardrail re-audit

| Guardrail | Status |
|---|---|
| No `place_order` / `modify_order` / `cancel_order` imports in new modules | **PASS** — router safety test passes; orchestrator safety test now hardened with `check=True` and PYTHONPATH (MF-5) |
| No `manage_watch_alerts` / paper / dry-run / live order creation | **PASS** — orchestrator only persists `TradingDecisionSession` + `TradingDecisionProposal`; no actions/counterfactuals/outcomes |
| `advisory_only=True` / `execution_allowed=False` stamped on every persisted payload | **PASS** — `_proposal_payload` (lines 316-317) and `market_brief` (lines 538-539); `ProposalPayload` schema constrains both to `Literal[True]/Literal[False]` |
| Decision Session is decision-ledger persistence only | **PASS** |
| TradingAgents pass-through advisory-only | **PASS** by exclusion — `include_tradingagents=True` still 501 |
| Live refresh is read-only | **PASS** — direct `select(KRSymbolUniverse)` is read-only; quote / orderbook / pending-orders fetchers all read-only; cash / holdings explicitly unavailable with warning |
| ROB-29 fail-closed for missing KR universe rows | **PASS** — MF-3 fix surfaces missing rows correctly through the classifier path |
| ROB-20 not touched | **PASS** — no edits to ROB-20 sources |
| No DB migration introduced | **PASS** |

---

## 4. Plan acceptance-criteria audit (final)

| Plan AC | Status |
|---|---|
| Accepts a Research Run UUID **or** clear selection criteria | ✅ `ResearchRunSelector` xor + `get_latest_research_run` |
| Refreshes only the live data needed for decision support | ✅ Quote / orderbook / KR universe / pending orders refreshed; cash / holdings unavailable with explicit warning |
| Proposal payload includes `research_run_id`, `refreshed_at`, `reconciliation_status`, `nxt_eligible` / `venue_eligibility` | ✅ All required keys + extras now declared in schema |
| Returns / verifies the Decision Session URL | ✅ Router returns URL via `build_trading_decision_session_url`; happy path no longer crashes |
| Existing Trading Decision Session flows continue to pass | ✅ 84-test sweep includes operator / SPA / router-safety / SPA-safety / session-URL / synth schemas |
| Forbidden-import safety tests | ✅ Router-safety + hardened service-safety; the prior-report duplicate is gone |
| ROB-29 fail-closed for missing KR universe rows | ✅ Provider correctly distinguishes missing-from-universe vs non-NXT |
| Implement strictly per the canonical plan | ✅ Out-of-scope alternate plan removed |

---

## 5. Carry-forward (non-blocking)

These were classified as Should-Fix or Low-priority in the prior report. None block this PR. Suggested follow-ups for a separate ticket:

- **SF-1** — `_build_pending_order_input` `side` fallback to `"buy"` could mis-classify in the (unlikely) case all three of live order / recon / candidate side are absent.
- **SF-2** — `LiveRefreshTimeout` declared but never raised; either implement end-to-end timeout or drop the class.
- **SF-3** — Verify `get_orderbook(symbol, market="crypto")` accepts the literal `"crypto"` (the underlying market_data API is documented for KR equity / KRW crypto only).
- **SF-4** — `_proposal_kind_from_candidate` lets a `holding` candidate's payload override `no_action` to e.g. `exit`. Cap by `candidate_kind` for safety.
- **SF-5** — Add a dedicated provider-import safety test (`tests/test_research_run_live_refresh_service_safety.py`) mirroring the router safety pattern.
- **LO-1..LO-5** — `LiveRefreshTimeout` getattr fallback dead code, `venue_eligibility.regular = True` always, missing `Location` header assertion in tests, `cash_balances` / `holdings_by_symbol` not surfaced to client response, `datetime.utcnow()` deprecation warnings.
- **MF-2 follow-up** — implement actual cash + holdings reads (currently signalled as `cash_unavailable` / `holdings_unavailable` warnings). File a ROB-25-followup issue.

---

## 6. Verdict

**PR-ready.** All 8 prior must-fix items resolved, both focused and broader test sweeps pass cleanly, no new safety regressions introduced. The follow-up commit `495819d1` is a correct async / eager-load fix that aligns the persisted session shape with the plan.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-25
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-25-review-report.md
AOE_NEXT: create_pr
