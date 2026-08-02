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

`UNIDENTIFIABLE` is *not* the same as `EXCLUDE`. The strict cohort builder admits
neither; the legacy calibration aggregate drops only explicit `EXCLUDE`, so
undecided legacy rows keep their existing behaviour (see §6).

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

## 6. Read models

- `build_eligible_forecast_calibration_aggregate(db, *, contract_version, predicate, …)`
  — both keyword arguments are **required with no default**. It returns the
  normal calibration groups *plus* `eligibility_counts`
  (`included`/`excluded`/`unidentifiable`), `eligibility_reasons`, and the
  `contract_version`, so a filtered cohort can never be presented as complete.
- `partition_by_eligibility(items, *, predicate)` — the pure gate, reused by
  trade-performance / PnL consumers so they apply the same rule.
- `build_forecast_calibration_aggregate(db, …)` (legacy entry point) now also
  drops forecasts whose **latest** revision says `calibration_exclude`. Rows with
  no decision are untouched — this is a re-entry guard, not a backfill.

`status = 'closed' AND brier_score IS NOT NULL` is explicitly **not** an
eligibility predicate.

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
- The trade-performance gate ships as the pure `partition_by_eligibility`
  predicate. No cross-lifecycle PnL aggregate over the Alpaca paper ledger exists
  in `main` today, so there is no second call site to wire; the next consumer
  applies the same predicate.
- Physical-account identity registration stays in `ROB-1204` and is untouched.
- Fill-observation projection (`ROB-1195`) is a separate lane; this contract
  consumes fill/position evidence as typed inputs and does not duplicate it.
