# ROB-25 — Review report

- Implementation commit reviewed: `ca2be013` *feat(research): create decision session from research run live refresh (ROB-25)*
- Plan: `docs/plans/ROB-25-research-run-decision-session-plan.md`
- Linear: ROB-25 (parent ROB-21)
- Worktree / branch: `feature/ROB-25-research-run-decision-session`
- Review verdict: **MUST FIX** (8 blocking, 5 medium, 5 low). Critical safety regressions and a runtime crash on the happy path.

---

## 1. Trading-safety guardrail audit

| Guardrail | Outcome | Evidence |
|---|---|---|
| No `place_order` / `modify_order` / `cancel_order` import in new modules | **PASS** (transitive) | Router safety test `tests/test_research_run_decision_session_router_safety.py` lists `orders_registration`, `orders_modify_cancel`, `paper_order_handler`, `kis_trading_service`, `kis_trading_contracts`, `fill_notification`, `execution_event`, `kis_websocket*`, `upbit_websocket`, `app.tasks` and currently passes. Source code review confirms no direct import of these in the orchestrator, provider, or router. |
| No `manage_watch_alerts` / paper / dry-run / live order creation | **PASS** | Orchestrator only calls `trading_decision_service.create_decision_session` and `add_decision_proposals`. No `record_decision_action`, no counterfactuals, no outcomes. |
| `advisory_only=True`, `execution_allowed=False` stamped on every persisted payload | **PASS** | Orchestrator `_proposal_payload` (lines 316–317) and `market_brief` (lines 524–525). `ProposalPayload` schema constrains both fields to `Literal[True]/Literal[False]`. |
| Decision Session is decision-ledger persistence only (not execution authorization) | **PASS** | No actions/counterfactuals/outcomes written; `notes`/`market_brief` carry advisory disclaimer text by structure. |
| TradingAgents pass-through, if invoked, must be `advisory_only=true` and `execution_allowed=false` | **PASS** by exclusion | `include_tradingagents=True` raises `NotImplementedError` → router maps to 501. Out of scope for v1, deferred to ROB-26. |
| Live refresh is read-only | **FAIL — see Must-Fix #3 (ROB-29 bypass) and gap on cash/holdings (Must-Fix #2)** | `is_nxt_eligible` returns `False` for both "non-NXT" AND "absent from universe" — silent fallback masks the ROB-29 fail-closed signal. Cash and holdings are stubbed. |
| ROB-20 not touched | **PASS** | No edits to ROB-20 sources. |
| No DB migration introduced | **PASS** | `git show --stat` confirms no `alembic/versions/*` change. |

The trading-safety surface is mostly intact, but **Must-Fix #3 silently disables ROB-29 fail-closed** in production-realistic scenarios. The pure-orchestrator unit test still passes only because it pre-supplies an empty `kr_universe_by_symbol`; the real provider would never produce that empty state. Until #3 is fixed, the end-to-end safety contract is broken.

---

## 2. Test report (independently re-run)

The implementer reported "32 passed, 15 warnings". I re-ran the same invocation in this worktree:

```bash
uv run pytest tests/test_research_run_schemas.py \
              tests/test_research_run_decision_session_schemas.py \
              tests/test_research_run_decision_session_service.py \
              tests/test_research_run_decision_session_service_safety.py \
              tests/test_research_run_live_refresh_service.py \
              tests/test_research_run_decision_session_router.py \
              tests/test_research_run_decision_session_router_safety.py -q
```

Result:
```
32 passed, 15 warnings, 11 errors in 9.43s
```

**The implementer's "32 passed" headline omits 11 ERRORs in `tests/test_research_run_decision_session_service.py`.** Every orchestrator unit test (happy-path KR / US / crypto, fail-closed, determinism, empty-candidates, NotImplementedError, user-isolation, by-criteria selector, by-uuid resolver) errors at fixture setup with:

```
sqlalchemy.exc.ProgrammingError: relation "users" does not exist
```

The `db_session` fixture (added in `tests/conftest.py`) connects to the project's PostgreSQL via `AsyncSessionLocal()` but does not run migrations or create schema. In any environment that doesn't already have the full Alembic schema applied (CI, fresh worktrees), the orchestrator's claimed coverage is **silently skipped**. The implementer's environment must have had a migrated DB; the rest of the team will not.

Coverage that **actually executes** today:

- `test_research_run_schemas.py` — pure schema (passes).
- `test_research_run_decision_session_schemas.py` — pure schema (passes).
- `test_research_run_decision_session_service_safety.py` — subprocess import check (passes — see Must-Fix #7 caveat).
- `test_research_run_live_refresh_service.py` — provider with full mock graph (passes).
- `test_research_run_decision_session_router.py` — router with **fully mocked** orchestrator/provider via `SimpleNamespace` (passes — see Must-Fix #1 caveat).
- `test_research_run_decision_session_router_safety.py` — subprocess import check (passes).

The orchestrator's actual DB integration, market_brief writeback, candidate ordering, fail-closed propagation, and pairing logic are **not exercised by passing tests**. The router test cannot catch contract drift between orchestrator and router because it mocks the orchestrator with a `SimpleNamespace` whose attribute set differs from the real `ResearchRunDecisionSessionResult` dataclass (see Must-Fix #1).

---

## 3. Must-Fix findings

### MF-1 (CRITICAL — runtime crash on happy path) — Router accesses non-existent attribute on the result dataclass

**File:** `app/routers/research_run_decision_sessions.py:101`

```python
return ResearchRunDecisionSessionResponse(
    session_uuid=result.session.session_uuid,
    session_url=session_url,
    status=result.session.status,
    research_run_uuid=result.research_run_uuid,    # <-- AttributeError
    ...
)
```

The dataclass `ResearchRunDecisionSessionResult` defined at `app/services/research_run_decision_session_service.py:34-41` has `research_run: ResearchRun`, `refreshed_at`, `proposal_count`, `reconciliation_count`, `warnings` — and **no** `research_run_uuid` field.

Why the test misses it: `tests/test_research_run_decision_session_router.py:70-78` builds a `SimpleNamespace(... research_run_uuid=mock_run.run_uuid, ...)` for the mocked result, so the attribute exists in test but not in real code. The first non-mocked call hits `AttributeError`.

**Fix:** either expose `research_run_uuid` as a property on the dataclass (`@property def research_run_uuid(self) -> UUID: return self.research_run.run_uuid`) or change the router to `result.research_run.run_uuid`. Add a router test that uses the real dataclass instance.

### MF-2 (CRITICAL — plan-required outputs are silently empty) — Live-refresh provider stubs cash & holdings

**File:** `app/services/research_run_live_refresh_service.py:183-185`

```python
# Fetch cash balances and holdings (simplified - mock implementation)
cash_balances: dict[str, Decimal] = {}
holdings_by_symbol: dict[str, Decimal] = {}
```

Plan §4.3 explicitly required the provider to fetch cash via `kis_holdings_service` / Upbit `fetch_balances` and holdings from the same source. Today these are dropped on the floor. The orchestrator's NXT holding classifier (`_build_nxt_item` line 269) silently falls back to `candidate.proposed_qty` for holdings, hiding the regression.

**Fix:** implement the read-only fetches per plan §4.3, or — if scoping forward — emit explicit `cash_unavailable` / `holdings_unavailable` warnings into `LiveRefreshSnapshot.warnings` AND propagate them into `market_brief.snapshot_warnings`, AND open a follow-up issue tagged ROB-25-followup. Don't ship empty-dict-pretends-to-be-real.

### MF-3 (CRITICAL — ROB-29 fail-closed bypassed in production) — Live-refresh KR universe call cannot distinguish "absent" from "non-NXT"

**File:** `app/services/research_run_live_refresh_service.py:124-136`

`is_nxt_eligible(symbol, db=db)` (defined at `app/services/kr_symbol_universe_service.py:363`) returns `False` for **both** "symbol present, NXT-ineligible" and "symbol entirely missing from `kr_symbol_universe`". The provider then writes:

```python
kr_universe_by_symbol[symbol] = KrUniverseSnapshot(nxt_eligible=False)
```

The classifier (ROB-22 `_resolve_nxt_actionable`) sees a present universe row with `nxt_eligible=False` → classifies as `kr_pending_non_nxt`, **never** `data_mismatch_requires_review`. The `missing_kr_universe` warning is never emitted. ROB-29 fail-closed is silently disabled along the live-refresh path.

The orchestrator's `test_missing_kr_universe_fail_closed` passes only because the test bypasses the provider and pre-supplies `kr_universe_by_symbol={}`. End-to-end with a real provider, no candidate ever lands in the empty-dict state.

**Fix:** in the provider, query `KRSymbolUniverse` directly (or add a `get_kr_universe_row(symbol)` helper that returns `KRSymbolUniverse | None`) and **omit** the entry from `kr_universe_by_symbol` when the row is missing or inactive. Add a provider unit test that simulates "symbol not in universe" and asserts the entry is absent and a `missing_kr_universe:{symbol}` warning is emitted. Add an end-to-end orchestrator test that uses the real provider against a seeded DB row (or its mock) to prove the classifier sees `kr_universe=None` and emits `data_mismatch_requires_review`.

### MF-4 (HIGH — orchestrator integration tests do not run in this worktree)

**File:** `tests/test_research_run_decision_session_service.py` + `tests/conftest.py`

11 tests error at the `db_session` / `user` fixtures with `relation "users" does not exist`. Pytest counts these as ERRORs, not FAILs, so the implementer's `-q` summary shows "32 passed" without a `0 failed` flag. The orchestrator's claimed coverage (happy-path, fail-closed propagation, market_brief content, deterministic ordering, user isolation, both selector forms) is **not enforced**.

Two compounding sub-issues:

1. The conftest `db_session` fixture relies on a pre-migrated PostgreSQL. CI must run `uv run alembic upgrade head` against a test DB before this test file, OR the fixture must seed schema itself (`Base.metadata.create_all` against an isolated test DB).
2. `tests/conftest.py` lines ~493-510 — `research_run_candidate_factory` constructs a `ResearchRunCandidate(...)` but **never `db_session.add`'s it, never flushes, never refreshes the parent run**. Even with a migrated DB, the candidates returned by the factory are detached and `run.candidates` stays empty. The orchestrator would raise `EmptyResearchRunError` on every "happy path" test.

**Fix:**
- Make the fixture set up schema deterministically (per-test transaction with `Base.metadata.create_all`, or a session-scoped `pytest-postgresql` test container, or document the explicit pre-test `alembic upgrade head` requirement).
- Fix `research_run_candidate_factory` to accept `db_session`, call `db_session.add(cand)`, `await db_session.flush()`, `await db_session.refresh(parent_run)`.
- Re-run the suite and confirm all 11 orchestrator tests **pass** (not error).
- Add a `tests/integration/` marker if these tests cannot run unconditionally.

### MF-5 (HIGH — service safety tests are false-positive prone)

**Files:**
- `tests/test_research_run_decision_session_service_safety.py`
- `tests/services/test_research_run_decision_session_service_safety.py`

Both files run a subprocess via `subprocess.run(cmd, capture_output=True, text=True, timeout=30)` **without `check=True`** and **without setting `cwd` or `PYTHONPATH`**. If the subprocess fails to import the module (rename, env error, sys.path mismatch), `result.stdout` is empty, `loaded` is `[]`, and `violations` is `[]` → the test silently passes despite providing **no actual safety check**.

Compare with `tests/test_trading_decisions_router_safety.py` and the new `tests/test_research_run_decision_session_router_safety.py`, which both set `check=True`, explicit `PYTHONPATH`, and explicit `cwd`. The router safety test is correctly hardened; the service safety tests are not.

**Fix:** mirror the router-safety test pattern — `check=True`, `cwd=project_root`, `env["PYTHONPATH"]=project_root`. Optionally also `assert result.returncode == 0` and assert `len(loaded) > 100` (sanity-check that the subprocess actually ran and loaded the standard library plus the module).

### MF-6 (HIGH — duplicate safety test files)

**Files:**
- `tests/test_research_run_decision_session_service_safety.py` (39 lines)
- `tests/services/test_research_run_decision_session_service_safety.py` (39 lines)

Both files are functionally identical (only the test function name differs). Plan §6.3 specified one. Two copies create maintenance drift.

**Fix:** delete the `tests/services/` copy. The plan only specified `tests/test_research_run_decision_session_service_safety.py`.

### MF-7 (HIGH — payload schema vs orchestrator output drift)

**File:** `app/schemas/research_run_decision_session.py:159-178` defines `ProposalPayload` with `model_config = ConfigDict(extra="forbid")` and a fixed key set.

**File:** `app/services/research_run_decision_session_service.py:315-372` emits these additional keys not present in the schema:

- `reconciliation_summary` (joined recon `reasons` string)
- `nxt_summary` (nxt_item summary string)
- `pending_order_id` (present in schema — OK)

If any consumer validates the persisted `original_payload` via `ProposalPayload`, validation fails with `extra fields not permitted`. The schema-orchestrator contract is broken.

**Fix:** decide which is canonical. Either (a) remove `reconciliation_summary` / `nxt_summary` from the orchestrator output (they're computable downstream from `decision_support` / `nxt_classification`), or (b) extend `ProposalPayload` to declare them. Add a schema-drift unit test that runs `ProposalPayload.model_validate(p.original_payload)` over a freshly created proposal.

### MF-8 (MEDIUM-HIGH — out-of-scope plan file committed)

**File:** `docs/superpowers/plans/2025-01-24-research-run-decision-session.md` (1585 lines)

The implementer added a parallel plan document outside the canonical path. The implementer briefing in the plan §10 said "Implement strictly per `docs/plans/ROB-25-research-run-decision-session-plan.md`" — there should be one canonical plan. Two plans drift, two plans contradict.

**Fix:** delete the duplicate planning artifact. If the implementer needs working notes, they belong in the PR description or in the canonical plan as a follow-up section.

---

## 4. Should-Fix (medium)

### SF-1 — `_build_pending_order_input` defaults `side` to `"buy"`

`app/services/research_run_decision_session_service.py:211`. When live order, recon, and candidate all lack a side, the fallback is `"buy"`. A sell-side proposal that loses its paired data is silently flipped to a buy classification. Default to a no-op classification or surface a `data_mismatch` reason instead.

### SF-2 — Provider does not enforce `LiveRefreshTimeout`

`app/services/research_run_live_refresh_service.py:30-33`. The class is declared and the router maps it to 504, but it is **never raised** by the provider. Plan §4.5 promised end-to-end timeout enforcement. Either implement (`asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout_seconds)`) or remove the class.

### SF-3 — `get_orderbook(symbol, market="crypto")` is unverified

The underlying `app.services.market_data.get_orderbook` documents support for "KR equity and KRW crypto markets". Whether it accepts the literal string `"crypto"` for `market_scope` is unverified; the provider doesn't normalize. The provider's only guard is `if run.market_scope == "us": return`. Crypto runs will call `get_orderbook` with `"crypto"` and may raise. Add a unit test that patches the upstream with the real call signature, or normalize the string before the call.

### SF-4 — `_proposal_kind_from_candidate` lets `holding` candidates promote to `enter`/`exit`

`app/services/research_run_decision_session_service.py:62-76`. If a `holding` candidate's payload includes `proposal_kind: "exit"`, the function returns `ProposalKind.exit` because the payload override wins over the kind_map. For a decision-ledger entry that promises "watch-only", this allows accidental promotion to an action-suggesting kind. Cap by `candidate_kind`: holdings always force `no_action`; pending orders always force `other`; only `proposed`/`screener_hit` may pull from payload.

### SF-5 — No live-refresh provider import-safety test

The provider is the only impure module and is allowed to import KIS/Upbit/market_data — but **must not** import mutation paths (`orders_modify_cancel.cancel_order_impl/modify_order_impl`, `kis_trading_service.place_order`, etc.). The router-safety test catches this transitively, but a dedicated `tests/test_research_run_live_refresh_service_safety.py` (mirroring `FORBIDDEN_MUTATION_PREFIXES` from the router safety test) would harden the boundary against future drift.

---

## 5. Low-priority observations

- **LO-1** — `app/routers/research_run_decision_sessions.py:23-27` uses `getattr(...)` to optional-import `LiveRefreshTimeout` with a `TimeoutError` fallback. The class is always present in the provider; the fallback is dead code.
- **LO-2** — `venue_eligibility.regular = True if research_run.market_scope in {"kr","us","crypto"} else None` (orchestrator line 311) is always True since `market_scope` is constrained to those three values by the model. Either compute meaningfully (e.g., from KR universe `is_active`) or drop the field.
- **LO-3** — Router `Location` header points at `/trading/api/decisions/{session_uuid}` (line 93-95). That endpoint exists in the existing `trading_decisions.py` router. Tests do not verify the header resolves; add a small assertion.
- **LO-4** — `ResearchRunDecisionSessionResponse` does not surface `cash_balances` / `holdings_by_symbol` to the client. Internal-only is fine but worth confirming with the API consumer (web SPA).
- **LO-5** — Many uses of `datetime.utcnow()` in the new tests trigger `DeprecationWarning` in Python 3.13. Switch to `datetime.now(UTC)`.

---

## 6. Additional focused tests required

Beyond fixing the existing tests (MF-4) and the schema-drift coverage (MF-7), add:

1. **Real-orchestrator router contract test** (covers MF-1): calls the router against a real (DB-backed) orchestrator instance, asserts the response includes `research_run_uuid` correctly. Cannot be mocked.
2. **Provider missing-from-universe test** (covers MF-3): patches `KRSymbolUniverse` to return `None` for one symbol; asserts the entry is OMITTED from `kr_universe_by_symbol` AND `missing_kr_universe:{symbol}` is in `warnings`. Then a layered orchestrator test that uses the real provider output and asserts the classifier emits `data_mismatch_requires_review`.
3. **Pending-order pairing test** (plan §6.2 required, currently missing): one candidate with `kind=pending_order` + matching `payload.order_id` + matching `ResearchRunPendingReconciliation`; assert the proposal payload's `pending_order_id` matches and `reconciliation_status` is the **refreshed** classification (not the persisted research-time one).
4. **`screener_hit` candidate kind test** (plan §6.2 required, currently missing): assert classification path goes through `classify_nxt_candidate` and proposal payload carries the right `nxt_classification`.
5. **`chasing_risk` end-to-end test**: feed a quote far enough above ordered price to trigger `chasing_risk`, assert the warning surfaces in both proposal payload and `market_brief.reconciliation_summary`.
6. **Provider safety import test** (covers SF-5).
7. **Cash/holdings happy-path test** (after MF-2 fix): assert provider populates `cash_balances` and `holdings_by_symbol` from mocked broker reads.
8. **Schema-drift test** (covers MF-7): `ProposalPayload.model_validate(p.original_payload)` round-trip over a freshly created proposal.

---

## 7. Plan acceptance-criteria audit

| Plan AC | Implementation | Status |
|---|---|---|
| Accepts a Research Run UUID **or** clear selection criteria | `ResearchRunSelector` xor implemented; `get_latest_research_run` helper added | **PASS** |
| Refreshes only the live data needed for decision support | Quote/orderbook/KR universe/pending-orders refreshed; **cash & holdings stubbed** | **FAIL — MF-2** |
| Proposal payload includes `research_run_id`, `refreshed_at`, `reconciliation_status`, `nxt_eligible` / `venue_eligibility` | All keys present in `_proposal_payload`; payload also adds undocumented `reconciliation_summary` / `nxt_summary` | **PASS** for required keys; **FAIL — MF-7** for schema drift |
| Returns / verifies the Decision Session URL | URL built; **router accesses non-existent attribute and crashes** | **FAIL — MF-1** |
| Existing Trading Decision Session flows continue to pass | No edits to `app/routers/trading_decisions.py`; existing tests not re-run by implementer report | **PARTIAL** — needs full sweep per plan §6.6 |
| Forbidden-import safety tests | Two service safety tests added — but **brittle** (no `check=True`/PYTHONPATH); router safety test correctly hardened | **PARTIAL — MF-5** |
| ROB-29 fail-closed for missing KR universe rows | Orchestrator unit-tested against pre-supplied empty dict; **provider does not produce the empty-dict state** | **FAIL — MF-3** |
| Implement strictly per the canonical plan | Implementer added a parallel plan file at `docs/superpowers/plans/2025-01-24-...md` | **FAIL — MF-8** |

---

## 8. Rollback assessment

If this commit must revert: 9 net new files plus the small `app/main.py` edit and the `get_latest_research_run` helper in `app/services/research_run_service.py`. No DB migration. No edits to ROB-22/23/24. `git revert ca2be013` is clean.

---

## 9. Summary

- **Trading-safety surface** is mostly correct in shape (advisory-only / execution-disallowed stamps, no broker mutation imports, no actions/counterfactuals/outcomes), but **ROB-29 fail-closed is silently disabled** along the live-refresh path because the provider can't tell "absent from universe" from "non-NXT eligible" (MF-3).
- **The router crashes on the first non-mocked call** because it reads `result.research_run_uuid` which doesn't exist on the dataclass (MF-1).
- **The orchestrator's claimed integration tests do not run** in any environment that doesn't already have a migrated PostgreSQL — and even with one, the candidate factory doesn't persist (MF-4). The "32 passed" headline omits 11 errors.
- **Cash and holdings** were promised by plan §4.3 and were stubbed (MF-2).
- **Service safety tests are false-positive prone** (MF-5) and **duplicated** (MF-6).
- **Schema and orchestrator output drifted** (MF-7).
- **An out-of-scope alternate plan was committed** (MF-8).

Block on the 8 must-fixes above. The 5 should-fixes (SF-1..SF-5) and 5 low-priority items can land in the same fix commit or be filed as follow-ups, at the implementer's discretion.

---

AOE_STATUS: review_must_fix
AOE_ISSUE: ROB-25
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-25-review-report.md
AOE_MUST_FIX_COUNT: 8
AOE_NEXT: start_fix_implementer
