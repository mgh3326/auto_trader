# ROB-24 — Code Review Report

**Linear issue:** ROB-24 — [Persistence] Add Research Run snapshot storage for KR/NXT preparation
**Branch:** `feature/ROB-24-research-run-snapshot-storage`
**Plan:** `docs/plans/ROB-24-research-run-snapshot-storage-plan.md`
**Reviewer model:** Claude Opus (read-only review)
**Verdict:** **PASSED** — no must-fix items.

---

## Files inspected

| Path | Status |
|------|--------|
| `alembic/versions/d34d6def084b_add_research_run_tables.py` | new |
| `app/models/research_run.py` | new |
| `app/models/__init__.py` | modified (exports added) |
| `app/schemas/research_run.py` | new |
| `app/services/research_run_service.py` | new |
| `tests/test_research_run_schemas.py` | new |
| `tests/models/test_research_run_models.py` | new |
| `tests/services/test_research_run_service.py` | new |
| `tests/services/test_research_run_service_safety.py` | new |
| `docs/plans/ROB-24-research-run-snapshot-storage-plan.md` | new |

`git status` confirms only the listed files were touched, with `app/models/__init__.py` as the only modified file in tracked state. No unrelated files altered.

---

## Acceptance criteria verification

### AC1 — Can create/load a Research Run with candidates and source freshness metadata
**Verdict: met.**
- `create_research_run` (`app/services/research_run_service.py:87`) accepts `source_freshness: dict[str, Any] | None`, persists it via the JSONB column, and returns the refreshed ORM row.
- `add_research_run_candidates` (`:122`) writes `ResearchRunCandidate` rows with their own optional `source_freshness` JSONB.
- `get_research_run_by_uuid` (`:185`) eager-loads candidates and reconciliations via `selectinload`, and enforces `(run_uuid, user_id)` ownership in the `WHERE` clause.
- Round-trip exercised by `test_create_research_run_with_candidates_and_reconciliations` and `test_get_research_run_by_uuid_enforces_ownership` — both green on the temp DB Hermes spun up.

### AC2 — Can attach pending reconciliation outputs
**Verdict: met.**
- `attach_pending_reconciliations` (`:153`) inserts `ResearchRunPendingReconciliation` rows from a typed dict, including ROB-22 `classification`, ROB-23 `nxt_classification`, gap_pct, reasons/warnings JSONB, decision_support JSONB, and the operator-facing summary.
- Adapter helpers `reconciliation_create_from_recon` (`:263`) and `reconciliation_create_from_nxt` (`:286`) translate ROB-22/ROB-23 DTOs into the persistence shape without re-classifying. `reconciliation_create_from_nxt` correctly raises `ValueError` when `kind != "pending_order"`, matching the plan's contract.
- Round-trip exercised by `test_adapter_from_recon_round_trip` and `test_adapter_from_nxt_round_trip`.

### AC3 — Records missing/stale source warnings
**Verdict: met.**
- Top-level `source_warnings` JSONB on `research_runs`, per-candidate `warnings` JSONB on `research_run_candidates`, and per-reconciliation `warnings` JSONB on `research_run_pending_reconciliations`. All three are `nullable=False` with `server_default '[]'::jsonb`.
- `test_create_research_run_with_candidates_and_reconciliations` asserts `run.source_warnings == ["missing_orderbook"]`.
- `test_adapter_from_recon_round_trip` asserts a per-row warning (`"missing_orderbook"`) round-trips through the adapter into the DB.

### AC4 — Does not create orders, watches, or order intents
**Verdict: met.**
- Static grep confirms the four ROB-24 source files contain no occurrence of `place_order`, `modify_order`, `cancel_order`, `manage_watch_alerts`, `register_watch`, `paper_order`, `order_intent`, or `fill_notification`.
- `tests/services/test_research_run_service_safety.py` runs a subprocess `sys.modules` audit and forbids `app.services.kis*`, `app.services.upbit*`, `app.services.brokers`, `app.services.order_service`, `app.services.orders`, `app.services.watch_alerts`, `app.services.paper_trading_service`, `app.services.openclaw_client`, `app.services.crypto_trade_cooldown_service`, `app.services.fill_notification`, `app.services.execution_event`, `app.services.kis_websocket*`, `app.services.kis_trading_*`, `app.services.kis_holdings_service`, `app.services.upbit_websocket`, `app.services.redis_token_manager`, `app.services.n8n_pending_orders_service`, `app.services.n8n_pending_review_service`, all `app.mcp_server.tooling.orders_*` / `order_execution` / `watch_alerts_registration`, plus `app.tasks` and `redis`. Test passes (11/11 in unit/safety run reproduced this review).
- The two ROB-22/23 imports in `research_run_service.py` are gated by `if TYPE_CHECKING:` (`:27-29`) so they are not pulled in at runtime; the adapters use string-form annotations under `from __future__ import annotations`. This keeps the runtime import surface minimal and is a correct pattern.

### AC5 — Stage values support `preopen`, `intraday`, `nxt_aftermarket`, future `us_open`
**Verdict: met.**
- Migration CHECK constraint `research_runs_stage_allowed` enumerates all four values.
- ORM check constraint matches (`app/models/research_run.py:96`).
- Pydantic `StageLiteral` matches (`app/schemas/research_run.py:16`).
- `ResearchRunStage` enum mirrors (`app/models/research_run.py:43`).
- `test_research_run_stage_check_rejects_unknown_value` exercises the DB-level CHECK; `test_run_create_rejects_unknown_stage` exercises Pydantic.
- `test_list_user_research_runs_filters_and_counts` writes a row with `stage="us_open"` to confirm the future stage already accepts.

### AC6 — Market scope supports `kr` end-to-end and design supports `us` / `crypto`
**Verdict: met.**
- Migration CHECK constraint `research_runs_market_scope_allowed` plus reconciliation row `research_run_pending_reconciliations_market_allowed` both enumerate `kr/us/crypto`.
- ORM mirrors. Pydantic `MarketScopeLiteral` mirrors.
- `test_research_run_market_scope_check_rejects_unknown_value` confirms `forex` is rejected at the DB.
- `test_list_user_research_runs_filters_and_counts` writes both `kr` and `us` rows and asserts filter behavior.

---

## Trading-safety guardrails

| Guardrail | Verdict | Evidence |
|-----------|---------|----------|
| No `place_order` / `modify_order` / `cancel_order` / watch alert / paper / dry-run / fill / live-order calls | ✅ | grep clean across the four source files; subprocess-import safety test enforced. |
| TradingAgents references advisory-only | ✅ | `_AdvisoryLink` schema pins `advisory_only: Literal[True]` and `execution_allowed: Literal[False]`; service-level `_validate_advisory_links` raises `ValueError` if either invariant is broken. Two test cases (`test_advisory_link_must_be_advisory_only`, `test_create_research_run_rejects_non_advisory_link`) exercise both directions. |
| Decision Session creation, watch registration, order placement not triggered by Research Run flow | ✅ | Service surface is purely INSERT + SELECT on the three new tables. No call into trading_decision_service, operator_decision_session_service, kis*, upbit*, watch_alerts, or any router/task module. |
| No secrets / API keys / tokens / account numbers handled or logged | ✅ | No `os.environ`, `settings.*_KEY`, account numbers, or token reads anywhere in the new code paths. |
| ROB-20 boundary respected (no live-refresh wiring, API/UI/Prefect) | ✅ | No new router, no Prefect flow, no UI template. Only persistence + adapter glue lands. |

---

## Migration safety

- `down_revision = "ce5d470cc894"` — descends from the prior head, matching plan and current `alembic heads`.
- Reuses the existing `instrument_type` PG enum via `postgresql.ENUM(..., name="instrument_type", create_type=False)`. No accidental ENUM recreation.
- `upgrade()` only `CREATE TABLE`s + `CREATE INDEX`es + FK creation; no destructive operations on existing data.
- `downgrade()` drops in reverse FK order: `research_run_pending_reconciliations` → `research_run_candidates` → `research_runs`. No leftover constraints reference the dropped tables.
- All non-nullable JSONB list/dict columns get `server_default '[]'::jsonb` / `'{}'::jsonb`, so raw-SQL inserts that omit them will not violate NOT NULL.
- Indexes on `(user_id, generated_at DESC)` and `(market_scope, stage, generated_at DESC)` align with the expected listing access patterns.
- Hermes verified upgrade/downgrade in isolation against a fresh temp DB stamped at `ce5d470cc894`. The unrelated `87541fdbc954` Timescale dependency is pre-existing and outside ROB-24's scope.
- After reapply, `\d research_run*` showed three tables; after rollback, `0` `research_run%` tables remained.

**Local re-verification this session:** `uv run alembic heads` reports `d34d6def084b (head)`, matching the migration filename.

---

## Hermes-applied fixes — review

Hermes corrected two DB-backed defects after the implementer handed off. Reviewing both:

### Fix 1 — `list_user_research_runs` aggregation
**Issue:** the original used `column("c.id")` / `column("r.id")` literals which SQLAlchemy could not resolve to actual columns, breaking the listing query end-to-end.
**Applied fix (now at `app/services/research_run_service.py:225-246`):** the aggregations are `func.count(distinct(ResearchRunCandidate.id))` and `func.count(distinct(ResearchRunPendingReconciliation.id))`, joined via two `outerjoin`s and grouped by `ResearchRun.id`.
**Reviewer assessment:** correct. Both child tables are joined off the same parent, so a naive `count(child.id)` would Cartesian-explode when both sides have rows; `count(distinct(...))` over each child correctly reports per-run counts in a single query (no N+1). The fix matches the pattern in `trading_decision_service.list_user_sessions` (`app/services/trading_decision_service.py:342-391`).
**Verification:** `test_list_user_research_runs_filters_and_counts` asserts `kr_row[1] == 1` (candidates) and `kr_row[2] == 0` (reconciliations) on a run with one candidate and zero reconciliations — exactly the case the buggy version would have miscounted.

### Fix 2 — JSONB Decimal serialization
**Issue:** the ROB-22/23 adapters return `decision_support` dicts that contain `Decimal` values (e.g. `Decimal("70140")`); `asyncpg` / SQLAlchemy refuse to JSON-encode `Decimal` directly into a JSONB column, so attaching adapter output blew up with a serialization error.
**Applied fix:** new helper `_json_safe(value)` (`app/services/research_run_service.py:63-70`) that recursively converts `Decimal` to `str`, normalizes `dict` keys to `str`, and converts `tuple` to `list`. It is applied at every JSONB write site:
- `_validate_advisory_links` per-link (`:83`).
- `create_research_run` for `market_brief` and `source_freshness` (`:110-111`).
- `add_research_run_candidates` for `source_freshness` and `payload` (`:141`, `:143`).
- `attach_pending_reconciliations` for `decision_support` (`:174`).
**Reviewer assessment:** correct, minimal, and well-targeted.
- Choice to stringify `Decimal` (rather than coerce to `float`) preserves precision — appropriate for prices and pct values that the plan specifically calls out as `Decimal`.
- Recursing into nested dicts/lists/tuples covers the realistic shapes (`decision_support` is a flat dict in ROB-22's `_empty_decision_support`, but ROB-23 / future callers may nest).
- Not a generic JSON encoder — it intentionally does not handle `datetime`, `bytes`, or other types. That's defensible because the plan specifies these JSONB columns hold opaque caller-supplied data, callers own ISO-8601 string conversion for timestamps, and datetimes already arrive as strings in the test fixtures (`"2026-04-28T05:00:00+00:00"`).
**Verification:** `test_adapter_from_recon_round_trip` and `test_adapter_from_nxt_round_trip` both feed `Decimal` values into `decision_support` via the adapters and assert the row inserts and reads back. They would fail with `decimal.Decimal is not JSON serializable` without the fix.

Both fixes are reasonable, scoped, and aligned with the plan's intent. No carry-forward concerns.

---

## Test coverage summary

| Area | Test file | Marker | Notes |
|------|-----------|--------|-------|
| Pydantic shape | `tests/test_research_run_schemas.py` | unit | 9 cases: minimum fields, unknown stage, unknown market scope, extra-field rejection, advisory-link invariant (both directions), symbol charset, confidence range, warning charset, two reconciliation shapes. |
| Pure-import safety | `tests/services/test_research_run_service_safety.py` | unit | subprocess `sys.modules` audit; passes on this session. |
| ORM & DB CHECK | `tests/models/test_research_run_models.py` | integration | round-trip, unknown stage rejected, unknown market_scope rejected, unknown classification rejected, cascade delete. |
| Service & adapters | `tests/services/test_research_run_service.py` | integration | create+candidates+reconciliations, ownership, list+filter+counts, recon adapter, NXT adapter, advisory-link invariant. |

Hermes confirmed: `uv run pytest tests/test_research_run_schemas.py tests/services/test_research_run_service_safety.py -q` → 11 passed (reproduced this session). DB-backed integration tests run on a temp Postgres DB → 11 passed.

The two reported Pydantic warnings originate from `app/auth/schemas.py:50,62` (`class-based config deprecated`) and are pre-existing — unrelated to ROB-24.

---

## Plan adherence

- Files added/modified match the plan's "File Structure" table exactly. No surprise files; no stray edits to ROB-22 / ROB-23 modules; no router / Prefect / UI / Operator-Decision wiring (deferred to ROB-25 per plan).
- `research_run_advisories` table is not introduced; the JSONB `advisory_links` column on `research_runs` carries TradingAgents references, matching the plan's deferral.
- Public service surface is exactly the five async functions plus two adapter helpers documented in the plan.
- Pydantic literal sets (`MarketScopeLiteral`, `StageLiteral`, `RunStatusLiteral`, `CandidateKindLiteral`, `ReconClassificationLiteral`, `NxtClassificationLiteral`) are byte-identical to the SQL CHECK constraints.

---

## Minor observations (informational only — not must-fix)

These are non-blocking; raised for awareness only.

1. **`reconciliation_create_from_nxt` side fallback.** When mapping an NXT classifier item with `side is None`, the adapter defaults to `"buy"` (`app/services/research_run_service.py:302`). This is safe under the ROB-23 invariant that pending-order kind always carries a side, but a tighter guard could `raise ValueError("nxt pending_order item missing side")` to prevent a silent mis-record if the upstream invariant ever drifts. Not blocking — current behavior matches plan, and ROB-23's own type contract makes the case unreachable today.

2. **`PendingReconciliationCreate.classification` from NXT-only adapter.** When constructed via `reconciliation_create_from_nxt`, the field is hard-coded to `"unknown"` because no ROB-22 result is paired. The plan called this out as expected behavior. Callers that have both ROB-22 + ROB-23 results in hand should overwrite this field — recommend documenting that pattern in the eventual ROB-25 wiring follow-up.

3. **Status not exposed on `create_research_run`.** The function does not accept a `status` parameter; rows always default to `"open"`. The plan never required a status setter for the first PR (no transition logic yet), so this is fine for ROB-24, but a future PR adding open→closed transitions will likely add a small updater function rather than a `status=` kwarg.

4. **`_json_safe` does not normalize `datetime` values.** Callers that drop `datetime` objects directly into `payload` / `source_freshness` / `decision_support` will hit a serialization error at insert time. The plan stated callers own ISO-8601 string conversion for timestamps, and the tests pass strings, so this is consistent. Worth noting in any future caller-facing docs so callers don't expect implicit serialization.

5. **`advisory_links` deduplication.** The service stores whatever list is passed; if a caller appends the same `session_uuid` twice, both rows persist. Acceptable for a first PR; a follow-up could enforce per-run uniqueness if duplication is observed.

None of the above warrant changes to land ROB-24.

---

## Final verdict

All 6 acceptance criteria are met, all trading-safety guardrails hold, the migration is reversible and isolation-verified, and the two Hermes-applied fixes are correct.

**No must-fix items. ROB-24 is ready to PR.**

AOE_STATUS: review_passed
AOE_ISSUE: ROB-24
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-24-review-report.md
AOE_NEXT: create_pr
