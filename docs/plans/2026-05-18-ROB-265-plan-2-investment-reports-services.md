# ROB-265 Plan 2 — Service Layer (repository, ingestion, query, decisions, watch activation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Add the service-layer operations that drive the `investment_*` schema added in Plan 1. Service-only: no MCP, no API routes, no scanner re-wire, no frontend, no broker mutation. Advisory-only invariants enforced app-side as defense-in-depth on top of the DB CHECKs.

**Architecture:**
- Pydantic v2 request schemas in `app/schemas/investment_reports.py` with `model_validator` enforcing the advisory-only invariants and the watch-item shape (`watch_condition` + `valid_until` required for `item_kind='watch'`).
- DAO at `app/services/investment_reports/repository.py` — narrow CRUD over the 5 tables, no business logic.
- Four business services on top of the repository, each in its own module: `ingestion`, `decisions`, `watch_activation`, `query_service`.
- All services are class-based, take an `AsyncSession` (matching the project pattern from `WatchOrderIntentService`).
- Idempotency keys composed via the Plan 1 helpers (`app/services/investment_reports/idempotency.py`).
- Watch activation copies the watch's metric/operator/threshold/etc into the alert's immutable snapshot fields (per locked refinement #3: items are source of truth, alerts are immutable activation snapshot).
- Tests against the real PostgreSQL `test_db` using the same `_ALL_TABLES` + truncate-between-tests pattern established in Plan 1.

**Tech Stack:** SQLAlchemy 2.x async (`AsyncSession`, `select`, `update`), Pydantic v2 (`BaseModel`, `Field`, `model_validator`), pytest-asyncio.

---

## File Structure

**Create:**
- `app/schemas/investment_reports.py` — `WatchConditionPayload`, `IngestReportItem`, `IngestReportRequest`, `RecordDecisionRequest`, `ActivateWatchRequest`, `PreviousContextFilter` Pydantic models with cross-field validators.
- `app/services/investment_reports/repository.py` — `InvestmentReportsRepository` class with CRUD methods over all 5 tables.
- `app/services/investment_reports/ingestion.py` — `InvestmentReportIngestionService`: idempotent report-bundle creation with deterministic key composition.
- `app/services/investment_reports/decisions.py` — `InvestmentReportDecisionService`: idempotent decision recording + item status transitions.
- `app/services/investment_reports/watch_activation.py` — `WatchActivationService`: copy approved watch item to `investment_watch_alerts` as immutable activation snapshot, idempotent per source item.
- `app/services/investment_reports/query_service.py` — `InvestmentReportQueryService`: list/get/latest + previous-report context.
- `tests/test_investment_reports_repository.py` — repository CRUD tests.
- `tests/test_investment_reports_ingestion.py` — ingestion idempotency, validator, advisory-only-at-schema tests.
- `tests/test_investment_reports_decisions.py` — decision recording, idempotency, status transition tests.
- `tests/test_investment_reports_watch_activation.py` — watch activation snapshot copy, idempotency, status-update, guard tests.
- `tests/test_investment_reports_query_service.py` — listing/filtering/latest/context-retrieval tests.

**Modify (light wiring only):**
- `app/schemas/__init__.py` — if it exists and pre-exports schema modules; add the new module's exports. (Verify pattern before changing.)

---

## Locked design decisions (carried from the conversation)

1. **Items are source of truth; alerts are immutable activation snapshot.** Activation copies these fields from item/report into alert: `market`, `target_kind`, `symbol`, `metric`, `operator`, `threshold`, `threshold_key`, `intent`, `action_mode`, `valid_until`, `rationale`, `trigger_checklist`, `max_action`. After copy, alert fields are not mutated except `status` and `updated_at`.

2. **Idempotency-key composition:**
   - Report: `report_key(report_type, market, market_session, kst_date, generator_version)`
   - Item: `item_key(report_uuid, item_kind, symbol, side, intent, watch_condition)`
   - Decision: `decision:{item_uuid}:{decision}:{actor}` (or caller-supplied)
   - Watch activation: `watch_activation_key(source_item_uuid)` (one activation per source item)
   - Watch event: not in this plan — Plan 4 (scanner re-wire)

3. **Status transitions** (decision → item status):
   - `approve` → `approved`
   - `deny` → `denied`
   - `defer` → `deferred`
   - `skip` → no status change (audit-only)
   - `partial_approve` → `approved` (the partial scope is recorded in `approved_payload_snapshot`)
   - `activate` (watch_activation, separate from decisions) → `activated`

4. **Advisory-only invariants** validated app-side at schema construction time AND enforced by DB CHECKs:
   - `account_scope='kis_live'` requires `execution_mode='advisory_only'`
   - `market_session='nxt'` requires `execution_mode='advisory_only'`

5. **No broker submission, no order mutation, no scanner write.** Services only persist analyst/operator decisions and watch activation snapshots. Plan 4 owns scanner integration.

6. **Previous-report context** is a query, not a single-link traversal. The `previous_report_uuid` column is a trace hint only. Context returns the recent N reports + unresolved deferred items + active watches + recent watch events + recent decisions, all by filter (market/market_session/account_scope/report_type).

---

## Task 1 — Pydantic schemas

**Files:**
- Create: `app/schemas/investment_reports.py`
- Create: `tests/test_investment_reports_schemas.py` (validator tests only — services arrive in subsequent tasks)

**Key types:**
- `WatchConditionPayload(metric, operator, threshold, threshold_key)` — Literal enums for `metric`/`operator`; threshold as `Decimal`.
- `IngestReportItem` with `model_validator(mode="after")` enforcing watch_condition + valid_until requirements for `item_kind='watch'`.
- `IngestReportRequest` with `model_validator(mode="after")` enforcing kis_live → advisory_only and nxt → advisory_only.
- `RecordDecisionRequest`, `ActivateWatchRequest` with optional `idempotency_key`.

**Tests:** schema-level rejection of (a) watch item without watch_condition, (b) watch item without valid_until, (c) kis_live with non-advisory execution_mode, (d) nxt session with non-advisory execution_mode. ~6-8 tests.

---

## Task 2 — Repository (DAO)

**Files:**
- Create: `app/services/investment_reports/repository.py`
- Create: `tests/test_investment_reports_repository.py`

**`InvestmentReportsRepository` methods (all `async`, all take primitives or model objects, return model objects):**
- Reports: `insert_report`, `get_report_by_id`, `get_report_by_uuid`, `get_report_by_idempotency_key`, `list_reports(filters, limit)`, `latest_report(filters)`
- Items: `insert_item`, `get_item_by_uuid`, `list_items_for_report`, `update_item_status`
- Decisions: `insert_decision`, `get_decision_by_idempotency_key`, `list_decisions_for_item`
- Alerts: `insert_alert`, `get_alert_by_idempotency_key`, `list_active_alerts(market=None, valid_at=None)`, `update_alert_status`
- Events: `insert_event`, `list_events_for_alert`, `list_recent_events(filters, since, limit)`

**Constraints:**
- No business logic — repository is just a thin SQLAlchemy wrapper.
- Filters on list methods support `market`, `market_session`, `account_scope`, `report_type`, `status`.
- `list_active_alerts` filters by `status='active'` and (if `valid_at` provided) `valid_until > valid_at`.

**Tests:** insert + retrieve round-trips per entity, idempotency-key lookups, list filtering. ~8-10 tests.

---

## Task 3 — Ingestion service

**Files:**
- Create: `app/services/investment_reports/ingestion.py`
- Create: `tests/test_investment_reports_ingestion.py`

**`InvestmentReportIngestionService.ingest(request: IngestReportRequest) -> InvestmentReport`:**
1. Compute report idempotency key via `report_key(...)`.
2. If `get_report_by_idempotency_key` returns a row, return it unchanged (no items re-inserted).
3. Otherwise: insert report; for each item in `request.items`, compute `item_key(...)`, and insert via repository.
4. Flush (do not commit — caller controls transaction).
5. Return the report.

**Tests:** ~6-8 tests
- Happy path: report + N items created.
- Idempotent re-ingest: second call returns same report_uuid, items unchanged (no duplicates).
- Watch item gets watch_condition stored as JSON.
- Validator pre-rejection: invalid request raises Pydantic ValidationError before DB hit.
- Mixed-kind items in one report (action + watch + risk).

---

## Task 4 — Decisions service

**Files:**
- Create: `app/services/investment_reports/decisions.py`
- Create: `tests/test_investment_reports_decisions.py`

**`InvestmentReportDecisionService.record(request: RecordDecisionRequest) -> InvestmentReportItemDecision`:**
1. Resolve item by uuid; raise if not found.
2. Compose idempotency_key (caller-supplied or auto: `decision:{item_uuid}:{decision}:{actor}`).
3. Idempotent: if `get_decision_by_idempotency_key` hits, return existing.
4. Insert decision row.
5. Transition item status per the mapping in "Locked design decisions" §3. `skip` does not transition.
6. Flush; return decision.

**Tests:** ~6-8 tests
- Decision recorded + item status transitions for approve/deny/defer/partial_approve.
- `skip` leaves item status unchanged.
- Idempotent re-record returns same decision_uuid, no new row.
- Multiple decisions per item (e.g., defer → approve) are allowed and both persist.
- Unknown item_uuid raises ValueError.

---

## Task 5 — Watch activation service

**Files:**
- Create: `app/services/investment_reports/watch_activation.py`
- Create: `tests/test_investment_reports_watch_activation.py`

**`WatchActivationService.activate(request: ActivateWatchRequest) -> InvestmentWatchAlert`:**
1. Resolve item by uuid; reject if missing, not `watch`, not `approved`, or missing `watch_condition`/`valid_until` (latter two enforced by DB but we validate early for clean errors).
2. Resolve owning report for `market` and `report_uuid`.
3. Compose idempotency_key as `watch_activation_key(source_item_uuid=item.item_uuid)` (or caller-supplied).
4. Idempotent: if alert exists by key, return existing.
5. Insert `InvestmentWatchAlert` copying immutable snapshot fields:
   - `market` from report
   - `target_kind`, `symbol`, `intent`, `rationale`, `trigger_checklist`, `max_action`, `valid_until` from item
   - `metric`, `operator`, `threshold`, `threshold_key` from item.watch_condition
   - `action_mode` from item.watch_condition (default to `notify_only` if not present)
6. Transition item status to `activated`.
7. Flush; return alert.

**Tests:** ~6-8 tests
- Happy path: alert created with snapshot fields populated from item + report, item status → `activated`.
- Idempotent re-activate returns same alert_uuid.
- Reject: item_kind != watch.
- Reject: item.status != approved.
- Reject: item not found.
- Snapshot is immutable: subsequent update to item fields doesn't change the alert.

---

## Task 6 — Query service (list/get/latest/context)

**Files:**
- Create: `app/services/investment_reports/query_service.py`
- Create: `tests/test_investment_reports_query_service.py`

**`InvestmentReportQueryService` methods:**

- `list_reports(market=None, market_session=None, account_scope=None, status=None, report_type=None, limit=20) -> list[InvestmentReport]` — ordered by `created_at` DESC.

- `latest_report(**filters) -> InvestmentReport | None` — same filters, returns the most recent matching `created_at`.

- `get_bundle(report_uuid: UUID) -> ReportBundle` — returns a Pydantic response wrapping the report + its items + decisions per item + active alerts derived from the report + recent events linked back to this report. Use a single round-trip where possible via `selectinload`/`joinedload` or explicit `IN` queries.

- `previous_report_context(market, market_session=None, account_scope=None, report_type=None, exclude_report_uuid=None, n_prior=3, since=None) -> PreviousContext` — query-based context per locked refinement #7. Returns:
  - `prior_reports`: top N matching reports (ordered DESC), excluding `exclude_report_uuid` if given
  - `unresolved_deferred_items`: items with `status='deferred'` from those prior reports
  - `active_watches`: active alerts whose `source_report_uuid` is in those prior reports' UUIDs and `status='active'`
  - `triggered_events`: events linked to those prior reports' UUIDs (`source_report_uuid IN`), most-recent N
  - `recent_decisions`: decisions on items from those prior reports, most-recent N

**Tests:** ~8-10 tests
- `list_reports` filters by each filter; default order by created_at DESC.
- `latest_report` returns most recent matching, or None.
- `get_bundle` returns nested items + decisions + alerts + events for a known report.
- `previous_report_context` returns prior reports, deferred items, active alerts, triggered events, and decisions correctly across multiple prior reports.
- `exclude_report_uuid` skips the named report from the prior set.
- Empty-state shapes (no prior reports): all collections are empty lists, no crash.

---

## Task 7 — Final wiring, lint, test sweep, PR

- Run `uv run ruff format <changed files>` and `uv run ruff check <changed files>`.
- Run `uv run ty check app/schemas/investment_reports.py app/services/investment_reports/`.
- Full Plan 2 test sweep: `uv run pytest tests/test_investment_reports_*.py -v`.
- Legacy guard: `uv run pytest tests/test_analysis_report_workflow.py tests/test_mcp_watch_order_intent_ledger.py tests/test_watch_order_intent_service.py -v`.
- Verify no broker/order side-effect imports added by greppping the new modules.
- Commit per-task with focused messages.
- Push branch `rob-265-plan-2`, open PR with **base `rob-265`** (stacked on Plan 1's PR).

---

## Self-review notes

- **No MCP/API/scanner/frontend touched.** Confirmed by inspecting the file list — only `app/schemas/`, `app/services/investment_reports/`, and `tests/` change.
- **Advisory-only invariant** validated app-side (`IngestReportRequest.model_validator`) AND DB-side (CHECK constraints from Plan 1). Defense in depth.
- **Watch activation** is idempotent per source item (one activation per approved watch). Subsequent activations return the existing alert without copying again — immutable snapshot guarantee.
- **No broker mutation:** services only INSERT/UPDATE `investment_*` tables. No imports of broker clients, KIS/Upbit/Alpaca/Kiwoom services.
- **Previous-context** is a query, matching locked refinement #7. `previous_report_uuid` remains a trace hint only.
- **Transaction boundaries** — services `flush()` but do not `commit()`. Callers (tests / future MCP handlers) own the transaction. Same pattern as `WatchOrderIntentService`.

---

## Out of scope for Plan 2

- MCP/API contract (`investment_report_*`) — Plan 3.
- OpenClaw notification payload update — Plan 3.
- Watch scanner re-wire that emits `investment_watch_events` — Plan 4.
- `/invest/reports` frontend — Plan 5.
- Legacy clean cut (drop `analysis_*` and `watch_order_intent_ledger`) — Plan 5.
