# ROB-265 Plan 5 — legacy clean cut + `/invest/reports` frontend + NXT pilot

> Stacked on Plan 4 (PR #870). This is the destructive cut + the frontend swap that closes out ROB-265.

**Goal:**
1. Delete the legacy `analysis_*` / `watch_order_intent_ledger` / OpenClaw watch surface — services, routers, MCP handlers, the legacy `watch_scanner` + `watch_proximity_monitor` jobs, the legacy `scan.watch_alerts` task, four ORM classes, four DB tables, and seven test files. ~5,000 LOC removed.
2. Add a `/invest/reports` + `/invest/reports/:reportUuid` SPA route that calls the Plan 3 HTTP endpoints. Replaces the deleted `/invest/action-center`.
3. End-to-end NXT advisory-only pilot test exercising report ingest → decide → activate → scan → Hermes notification with `market_session='nxt'`, `account_scope='kis_live'`, `execution_mode='advisory_only'`.

**Locked semantics:**
- Watch remains a **review trigger**, never an automatic order instruction.
- Hermes naming preserved everywhere. No new OpenClaw contract names. The OpenClaw screener callback (`OPENCLAW_SCREENER_CALLBACK_URL`) is **unrelated to ROB-265's watch path** and stays.
- NXT remains advisory-only — DB CHECK constraints (Plan 1) enforce; pilot test verifies end-to-end.
- No broker / live order mutation introduced.

---

## Removal manifest

**Backend services (delete):**
- `app/services/analysis_report_service.py` (318)
- `app/services/watch_order_intent_service.py` (323)
- `app/services/openclaw_client.py` (618)

**Schemas + routers (delete):**
- `app/schemas/analysis_reports.py` (169)
- `app/routers/analysis_reports.py` (118)
- `app/routers/watch_order_intent_ledger.py` (113)

**MCP handlers (delete):**
- `app/mcp_server/tooling/analysis_reports_handlers.py` (152)
- `app/mcp_server/tooling/watch_order_intent_ledger_read.py` (85)

**Jobs (delete):**
- `app/jobs/watch_scanner.py` (495)
- `app/jobs/watch_proximity_monitor.py` (315)

**Tasks:**
- Delete `app/tasks/watch_proximity_tasks.py` (17)
- Remove `run_watch_scan_task` from `app/tasks/watch_scan_tasks.py` (keeps `run_investment_watch_scan_task`)

**Wiring:**
- `app/main.py`: drop the `analysis_reports`, `watch_order_intent_ledger` imports + `include_router` calls.
- `app/mcp_server/tooling/registry.py`: drop both `register_*_tools` imports + calls.
- `app/tasks/__init__.py`: drop `watch_proximity_tasks` if listed.

**Models (in `app/models/review.py`):** remove `WatchOrderIntentLedger`, `AnalysisReport`, `AnalysisStageResult`, `AnalysisOrderCandidate`. Update `app/models/__init__.py` to drop any exports.

**Migration:** new `alembic/versions/20260519_rob265_drop_legacy_action_center.py` against head `20260519_rob265_delivery`. DROP TABLE (CASCADE for safety) on:
- `review.analysis_order_candidates`
- `review.analysis_stage_results`
- `review.analysis_reports`
- `review.watch_order_intent_ledger`

`downgrade()` is a `raise NotImplementedError("destructive cut")` — the legacy schema is intentionally not recoverable from this migration.

**Tests (delete):**
- `tests/test_analysis_report_workflow.py`
- `tests/test_mcp_watch_order_intent_ledger.py`
- `tests/test_watch_order_intent_service.py`
- `tests/test_watch_order_intent_preview_builder.py`
- `tests/test_watch_scanner.py`
- `tests/test_openclaw_client.py`
- `tests/jobs/test_watch_proximity_monitor.py`

Any other test importing the deleted modules — to be deleted or rewritten. Use `grep` after the source delete to find them.

**Config (in `app/core/config.py`):** delete `OPENCLAW_WEBHOOK_URL`, `OPENCLAW_TOKEN`, `OPENCLAW_CALLBACK_TOKEN`, `OPENCLAW_CALLBACK_URL`, `OPENCLAW_ENABLED`. **Keep** `OPENCLAW_SCREENER_CALLBACK_URL` (unrelated screener path).

**Cross-refs to adapt (NOT delete):**
- `app/jobs/daily_scan.py` — remove the OpenClaw import + `self._openclaw` init. Keep the rest (screener flow).

---

## Frontend swap

**Delete:**
- `frontend/invest/src/api/actionCenter.ts`
- `frontend/invest/src/hooks/useActionCenter.ts`
- `frontend/invest/src/types/actionCenter.ts`
- `frontend/invest/src/pages/desktop/DesktopActionCenterPage.tsx`
- `frontend/invest/src/pages/mobile/MobileActionCenterPage.tsx`
- `frontend/invest/src/components/action-center/*` (5 files)
- `frontend/invest/src/__tests__/DesktopActionCenterPage.test.tsx`
- `frontend/invest/src/__tests__/actionCenter.api.test.ts`
- Remove `/invest/action-center` route from `frontend/invest/src/routes.tsx`

**Add:**
- `frontend/invest/src/types/investmentReports.ts` — TS types mirroring the Plan 3 response shapes (camelCase).
- `frontend/invest/src/api/investmentReports.ts` — API client with snake → camel normalization (matches `actionCenter.ts` style). Calls `GET /invest/api/investment-reports` and `GET /invest/api/investment-reports/{uuid}`. `credentials: "include"`.
- `frontend/invest/src/hooks/useInvestmentReports.ts` — list hook.
- `frontend/invest/src/hooks/useInvestmentReportBundle.ts` — detail hook.
- `frontend/invest/src/components/investment-reports/InvestmentReportsContent.tsx` — list view.
- `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` — detail view with items grouped by kind, decisions per item, alerts, recent events. **Surface `delivery_status` on each event** so operators see "delivered / skipped / failed" — the Plan 4 hardening payoff.
- `frontend/invest/src/pages/desktop/DesktopInvestmentReportsPage.tsx` + mobile.
- `frontend/invest/src/pages/desktop/DesktopInvestmentReportBundlePage.tsx` + mobile.
- Routes in `frontend/invest/src/routes.tsx`: `/invest/reports`, `/invest/reports/:reportUuid`.
- `frontend/invest/src/__tests__/investmentReports.api.test.ts` — API client normalization.
- `frontend/invest/src/__tests__/InvestmentReportsContent.test.tsx` — page rendering, loading/error/empty states.

**Reuse:** `DesktopShell`, `MobileShell`, `Card` from the design system. Match inline Korean labels (`"확인 불가"`, `"투자 리포트"`, etc.) — no i18n introduced.

---

## NXT advisory-only pilot end-to-end test

`tests/test_nxt_advisory_only_pilot.py`: one focused integration test that exercises the entire stack:
1. Ingest a report with `market='kr'`, `market_session='nxt'`, `account_scope='kis_live'`, `execution_mode='advisory_only'` containing an action item + a watch item.
2. Record `approve` decisions on both items.
3. Activate the watch.
4. Stub Hermes to return success. Stub market-data to return a value that triggers the watch.
5. Run `InvestmentWatchScanner.scan_market("kr")`.
6. Assert: event row persisted with `delivery_status='delivered'`, alert → `triggered`, Hermes received one call with the correct snapshot.
7. Verify zero broker / order side effects — grep would be circular here, so assert via the test that no `OPENCLAW_*` calls happen (the OpenClaw client is deleted) and that no `place_order` / `kis_*` MCP tool was called.

---

## Tasks

### Task 1 — Backend legacy cut: delete modules + wiring + config
Delete 12 backend files. Edit `app/main.py`, `app/mcp_server/tooling/registry.py`, `app/tasks/watch_scan_tasks.py`, `app/tasks/__init__.py`, `app/core/config.py`. Run import-check via `uv run python -c "import app.main"` to verify nothing else is left dangling.

### Task 2 — Models + alembic drop migration
Remove 4 classes from `app/models/review.py`. Update `app/models/__init__.py`. Write the drop migration. Apply locally; verify the 4 tables are gone and the schema is consistent with the new ORM via `uv run alembic upgrade head`.

### Task 3 — Delete legacy tests
Delete 7 test files. Re-run a full pytest sweep — if anything else imports the legacy modules, delete or rewrite it.

### Task 4 — Adapt daily_scan.py
Remove the OpenClaw import + init from `app/jobs/daily_scan.py`. Existing daily_scan tests stay valid.

### Task 5 — Frontend: new investment-reports surface
Add types, API client, hooks, components, pages, routes, tests. Match the existing action-center conventions (DesktopShell/MobileShell, snake→camel normalization, inline Korean, `credentials: 'include'`).

### Task 6 — Frontend: delete action-center surface
Delete 12 frontend files. Remove the `/invest/action-center` route.

### Task 7 — NXT advisory-only pilot test
Write `tests/test_nxt_advisory_only_pilot.py` exercising the full ingestion → decision → activation → scan → Hermes flow.

### Task 8 — Final verification + PR
- `ruff format` + `ruff check` clean
- `ty check` clean on changed Python modules
- Full backend pytest sweep — only the new `investment_*` + `test_nxt_advisory_only_pilot.py` tests should be present (legacy tests are gone)
- `npm test` clean in `frontend/invest/`
- `alembic upgrade head` on a fresh DB applies the full chain cleanly
- Grep for `analysis_report`, `watch_order_intent`, `OpenClawClient`, `WatchScanner`, `WatchOrderIntentService` across `app/` — expect zero hits in non-deleted files (except `daily_scan.py` mentions in docstrings/comments — clean those if needed)
- Pre-drop row count SQL block included in the PR body (operator runs this on staging before merge)
- Push `rob-265-plan-5`, open PR with base `rob-265-plan-4`

---

## Out of scope

- Hermes inbound callback (operator decision → auto_trader): MCP `investment_report_decide_item` already handles this.
- Outbox retry job: Plan 4's hardening means a stuck `failed`/`skipped` row is re-tried by the next scan loop. A dedicated outbox is future work.
- Re-arm of triggered watches: a re-arm requires a new `investment_report_activate_watch` call. Plan 5 surfaces this in the UI but doesn't introduce a one-click re-arm yet.
- A separate operator UI for the OpenClaw screener callback path: untouched.
