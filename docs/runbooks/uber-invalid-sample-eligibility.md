# UBER invalid-sample eligibility — `uber-invalid-sample-eligibility.v1`

ROB-1036. Additive eligibility record + contract-compatible post-fill writer for the
`alpaca_paper_lab` UBER `invalid_sample_cleanup` case.

This runbook describes **what exists in code**. It does not authorise an order, a
cleanup retry, a forecast resolve, a migration apply, or a scheduler registration.

---

## 1. Why this exists

The 2026-07-30 readiness audit
(`herdr-inbox/uber-d2-execution-readiness-2026-07-30.md`) and the 2026-07-31
result (`…-execution-result-2026-07-31.md`) recorded
`EXECUTION_RESULT=BLOCKED_STATIC_CONTRACT_GAP` / `SUBMIT_RESULT=NO_SUBMIT`. The
blocker was not the order boundary — it was that there was no way to record
"this sample is invalid for scoring" without either

- calling `forecast_resolve(dry_run=false)`, which books outcome + Brier and
  therefore violates *exclude from strategy performance*, or
- not calling it, which leaves the approved post-fill step unfinished.

This contract removes that dilemma by making eligibility a separate, append-only
record instead of a side effect of resolution.

## 2. The four validity domains

They are **four questions, not one flag**. Collapsing them is a contract
violation, and `EligibilityDecision` deliberately exposes no aggregate bit.

| Domain | Type | Question |
|---|---|---|
| `forecast_outcome_observability` | `ForecastOutcomeObservability` | May the outcome be observed/resolved? |
| `calibration_eligibility` | `CalibrationEligibility` | Does it enter the calibration primary cohort? |
| `trade_performance_eligibility` | `TradePerformanceEligibility` | Does it enter PnL / trade-performance aggregates? |
| `operational_reliability_eligibility` | `OperationalReliabilityEligibility` | Does it count towards operational reliability? |

The four enums have disjoint value sets, so a cross-domain assignment raises
`TypeError` rather than silently type-checking.

**The fixed UBER decision** (operator, carried in
`query-codexmock-orch-strategy-roadmap-2026-07-31-2333.md` §2.2):

```
forecast_outcome_observability     = blocked_pending_audit_evidence   # retained, not discarded
calibration_eligibility            = calibration_exclude
trade_performance_eligibility      = trade_performance_exclude
operational_reliability_eligibility= operational_include              # the attempt is a real operational event
```

## 3. Fail-closed default

A subject with **no** decision row is `UNIDENTIFIABLE` in all four domains. There
is no `COALESCE(..., INCLUDE)` and no historical backfill: the migration inserts
zero rows, and `unidentifiable_decision()` is the only default.

`UNIDENTIFIABLE` is *not* the same as `EXCLUDE`. An explicit `EXCLUDE` is held out
by every cohort; an undecided row's treatment is a property of the cohort a
caller names (see §6).

## 4. Append-only revisions

A correction is a **superseding revision**, never an overwrite.

- `revision_no` starts at 1; every later revision has
  `supersedes_revision_no = revision_no - 1` (CHECK constraint) — a gap, a
  branch, and a cycle are all unrepresentable.
- `(subject_kind, subject_ref, revision_no)` is unique — no branch.
- a partial unique index on `supersedes_revision_no` blocks two revisions
  claiming the same predecessor.
- `BEFORE UPDATE OR DELETE OR TRUNCATE` triggers reject mutation at the DB edge.
- `evidence_hash` is a SHA-256 over the canonical JSON evidence and is recomputed
  on every read; a mismatch raises `evidence_hash_mismatch` instead of returning
  the row.

Only the **latest** revision decides. A later revision may legitimately readmit a
previously excluded subject once audit-grade evidence lands.

## 5. Cleanup binding

`review.invalid_sample_cleanup_bindings` immutably binds, in one row:

`purpose=invalid_sample_cleanup` · `forecast_id` + `sample_ref` ·
`approval_id` + `approval_hash` + `approval_expires_at` + `approval_session_id` ·
`mission_id` · `account_mode` + `client_order_id` + `lifecycle_correlation_id`
· `binding_hash` (SHA-256 over all of the above).

`build_cleanup_binding()` fails closed on:

| code | condition |
|---|---|
| `naive_now` / `naive_approval_expires_at` | a non-timezone-aware clock |
| `approval_window_expired` | `now >= approval_expires_at` — a missed window is **not** carried forward |
| `cross_session_carry_over_blocked` | the approval belongs to another session |
| `conflicting_binding_for_client_order_id` | that identity is already bound to a different approval/mission |

Re-authoring the *identical* binding is idempotent.

## 6. Read models — cohorts (operator decision D-1, option ②)

Every calibration call must **name its cohort**. There is no default:

```python
build_forecast_calibration_aggregate(
    db, *, contract_version: str, predicate: EligibilityPredicate, ...
)
```

Both are keyword-only with no default, so `status = 'closed' AND brier_score IS
NOT NULL` can never again stand in for a cohort definition. The result carries
the cohort back:

| field | meaning |
|---|---|
| `contract_version` | which eligibility contract was asked for |
| `eligibility_cohort` | the cohort label that produced these numbers |
| `eligibility_stage` | `rob-1036-d1-option-2-compatibility` or `rob-1036-decided-only` |
| `eligibility_cohort_admits` | the exact admitted values |
| `eligibility_counts` | `included` / `excluded` / `unidentifiable`, **kept separate** |
| `eligibility_admitted_count` | how many rows entered the aggregate |
| `eligibility_reasons` | per-value tally of what was held back |

🔴 The three counts are **never summed** into one "eligible" number. Under the
compatibility cohort the undecided rows are admitted *and* counted, and showing
that quantity is the entire reason this cohort exists.

### 6-1. The two shipped cohorts

`COMPATIBILITY_CALIBRATION_COHORT` (`app/services/invalid_sample_eligibility/cohort.py`)
admits `{INCLUDE, UNIDENTIFIABLE}`. It is what the three live call sites use:

| call site | file:line |
|---|---|
| MCP `get_forecast_calibration` | `app/mcp_server/tooling/forecast_tools.py:213-217` |
| `GET /trading/api/invest/forecasts/calibration` | `app/routers/invest_forecasts.py:61-67` |
| decision-history running Brier | `app/services/decision_history.py:122-137` |

`DECIDED_ONLY_CALIBRATION_COHORT` admits `{INCLUDE}` only — the end state. It is
available today (`build_decided_only_forecast_calibration_aggregate`) so a caller
that wants the fully-decided cohort can ask for it and the promotion is a
one-line swap per call site.

An explicit `calibration_exclude` is held out by **both**. Widening admission
never resurrects an excluded sample.

### 6-1-1. 🔴 This is a transitional stage, not the end state

The compatibility cohort exists so the calibration surfaces keep working while
the undecided population becomes visible. It is deliberately **not** presented as
a final or "correct" configuration.

**Termination condition** (also in `cohort.py`, next to the constant):

1. every forecast in the scored population carries an explicit eligibility
   decision recorded through `InvalidSampleEligibilityService` — recorded by an
   operator, never by an automatic historical backfill (§4.2-4); **and**
2. the reported `unidentifiable` count has been 0 for a full review cycle on the
   cohorts an operator relies on.

Then the call sites move to `DECIDED_ONLY_CALIBRATION_COHORT` and the
compatibility cohort is deleted.

**Promotion provenance.** Option ② is a superset of the end state — same
machinery, wider admitted set — so no door is closed. Because every result is
stamped with `eligibility_cohort` / `eligibility_stage` /
`eligibility_cohort_admits`, a number produced during this stage stays
distinguishable from a post-promotion number, and a before/after comparison
report over the same population remains possible. Classification is independent
of admission, so the counts mean the same thing under both cohorts and are
directly comparable.

### 6-2. Trade performance (ROB-1036 B2)

`TradePerformanceEligibility` is wired into the real Alpaca PnL path:

- `InvalidSampleEligibilityService.list_trade_performance_excluded(correlation_ids)`
  resolves explicit `trade_performance_exclude` decisions.
- `paper_evaluation/evidence.py` — `_trade_performance_excluded_row_ids()` turns
  those into **native row ids**, and `_load_native` skips them when building
  `alpaca_fills`. Row ids (not correlation ids) are used so the ROB-850
  assignment-scoping guard on `_load_native` stays intact: nothing is discovered
  by correlation id, an already-discovered row is merely filtered out.
- `paper_evaluation/pnl.py` — `compute_alpaca_view(..., excluded_correlation_ids=…)`
  skips both the correlation bucket and any individual row carrying an excluded
  lifecycle id.

Only an explicit `EXCLUDE` filters. An undecided lifecycle is `UNIDENTIFIABLE`
and stays, so with no decisions on record the PnL inputs are unchanged.

## 7. Post-fill completion gate

`evaluate_post_fill_completion(fill_evidence=…, position_effect=…)` returns
`COMPLETE` only when the broker fill evidence is `COMPLETE` **and** the position
effect is `CONSISTENT`. Exactly 1 of the 9 combinations is terminal success; the
other 8 are typed manual-review reasons:

`absent_fill_evidence` · `incomplete_fill_evidence` ·
`absent_position_effect_evidence` · `inconsistent_position_effect_evidence`

A `filled` broker status with an incomplete activity set, or a complete fill with
no observed position change, is a refusal — never a silent success.

## 8. Timeout / crash recovery

`plan_timeout_recovery(client_order_id=…, evidence_lookup_available=…)` returns a
plan whose only actions are `lookup_existing_order_evidence` or
`escalate_manual_review`. There is no resend variant; `broker_post_allowed` and
`new_identity_allowed` are `Literal[False]` properties, not flags. The original
`client_order_id` is carried through both branches so no new identity can be
minted.

In this change this is proven with a **fake transport only** — no broker call was
made (`tests/…/test_post_fill.py::test_timeout_recovery_reads_the_same_identity_and_posts_nothing`).

## 9. Write surface

`InvalidSampleEligibilityService` is the only writer. Static guards
(`tests/services/invalid_sample_eligibility/test_static_boundaries.py`) enforce:

- the repository is imported by `service.py` and nothing else;
- `contract.py` / `post_fill.py` / `binding.py` import no SQLAlchemy, HTTP
  client, or broker module;
- no raw SQL write statement anywhere in `app/` names these tables;
- the service performs no `update`/`delete`/`execute` call;
- the package imports no broker / MCP / Alpaca order surface.

## 10. What this change does NOT do

- ❌ no order, preview, cancel, reconcile, position read, or any broker/account call
- ❌ no UBER outcome / price / Brier lookup, computation, or `forecast_resolve`
- ❌ no cleanup retry, and no carry-over of the missed 2026-07-30 15:55 ET window
- ❌ no migration applied to the runtime/production database (see §11)
- ❌ no scheduler / cron / TaskIQ / Prefect registration; no deploy; no activation
- ❌ no historical backfill — the migration inserts zero rows
- ❌ no MCP tool registered

## 11. Operator cutover

The migration ships in the PR but is **not** applied by it.

```bash
uv run alembic current            # expect 20260728_rob1109_watch_intent
uv run alembic upgrade head       # applies 20260802_rob1036_sample_elig
uv run alembic downgrade -1       # rollback (drops the three tables + trigger fn)
```

The upgrade is additive: three new `review` tables, their indexes, and the
`review.reject_invalid_sample_mutation()` trigger function. No existing table,
column, constraint, or row is touched. Downgrade drops exactly what upgrade
created; because the tables are append-only, a downgrade **destroys** any
decisions recorded in between — export them first if any exist.

Verified in an isolated throwaway database by
`tests/services/invalid_sample_eligibility/test_migration.py::test_isolated_upgrade_downgrade_upgrade`
(upgrade → trigger check → downgrade → upgrade).

## 12. Open / deferred

- Recording the actual UBER decision row is an **operator action**, not part of
  this change.
- Operator decision **D-1 selected option ②** (2026-08-02): every calibration
  call names its cohort, and the default cohort admits undecided samples while
  reporting them separately (§6). The termination condition for that stage is in
  §6-1-1 — this is transitional, not the end state.
- The trade-performance gate is wired into the Alpaca PnL path (§6-2). Other
  aggregates (`trade_journal/aggregates.py`, keyed by symbol/tag over
  `review.trades`) have no forecast/lifecycle identity to join on and are
  untouched; a future consumer applies the same predicate.
- Physical-account identity registration stays in `ROB-1204` and is untouched.
- Fill-observation projection (`ROB-1195`) is a separate lane; this contract
  consumes fill/position evidence as typed inputs and does not duplicate it.
