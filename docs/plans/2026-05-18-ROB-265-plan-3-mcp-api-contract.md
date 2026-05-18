# ROB-265 Plan 3 — MCP/API contract for `investment_reports`

> Stacked on Plan 2 (PR #865). Service layer is unchanged; this plan only
> adds the user-facing read/write surface and the operator MCP tools.

**Goal:** Expose the Plan 2 service layer through (a) HTTP read endpoints for the dashboard/Hermes consumers, and (b) MCP tools for operator workflows. The six tool names follow the Linear issue exactly. No broker mutation; legacy `analysis_*` and `watch_order_intent_ledger_*` registrations stay in place — clean cut is Plan 5.

**Architecture:**
- HTTP routes: GET-mostly read surface at `/trading/api/investment-reports/...` + `/invest/api/investment-reports/...` (so the `/invest` frontend can call the same data, matching the legacy alias pattern).
- MCP handlers: 6 tools — `investment_report_create`, `investment_report_list`, `investment_report_get`, `investment_report_decide_item`, `investment_report_activate_watch`, `investment_report_context_get`. Each opens its own `AsyncSessionLocal`.
- Pydantic response models in `app/schemas/investment_reports.py` use `from_attributes=True` to convert ORM rows. The router & MCP both serialise through these, so the JSON shape is consistent across surfaces.
- Wiring: `app/main.py` includes the new router; `app/mcp_server/tooling/registry.py` calls a new `register_investment_report_tools`.
- OpenClaw payload schema work is deferred to Plan 4 (it's scanner-driven and only meaningful once the scanner emits `investment_watch_events`).

**Tech stack:** FastAPI (existing patterns), Pydantic v2 (existing), pytest-asyncio + httpx TestClient.

---

## File Structure

**Create:**
- `app/routers/investment_reports.py` — APIRouter with 3 GET routes
- `app/mcp_server/tooling/investment_reports_handlers.py` — 6 tool impls + `register_investment_report_tools`
- `tests/test_investment_reports_router.py` — TestClient against a minimal FastAPI app
- `tests/test_investment_reports_mcp.py` — direct calls to the `*_impl` functions

**Modify:**
- `app/schemas/investment_reports.py` — append response Pydantic models
- `app/main.py` — `from .routers import investment_reports` + `include_router`
- `app/mcp_server/tooling/registry.py` — import + call `register_investment_report_tools(mcp)`

---

## HTTP routes (all under `tags=["investment-reports"]`)

| Method | Path | Response model | Calls |
|---|---|---|---|
| GET | `/trading/api/investment-reports` | `InvestmentReportListResponse` | `QueryService.list_reports` |
| GET | `/invest/api/investment-reports` | same | same |
| GET | `/trading/api/investment-reports/context` | `PreviousReportContextResponse` | `QueryService.previous_report_context` |
| GET | `/trading/api/investment-reports/{report_uuid}` | `InvestmentReportBundle` | `QueryService.get_bundle` |
| GET | `/invest/api/investment-reports/{report_uuid}` | same | same |

Routes are auth-required (`get_authenticated_user` Depends) matching the legacy `analysis_reports.py`. Write paths (create/decide/activate) go through MCP only in Plan 3 — HTTP write endpoints can be added later if needed.

## MCP tools (6, all registered together)

| Tool | Wraps | Notes |
|---|---|---|
| `investment_report_create` | `IngestionService.ingest` | Idempotent. Accepts an `IngestReportRequest` payload as primitives. |
| `investment_report_list` | `QueryService.list_reports` | Filters: market, market_session, account_scope, status, report_type, limit. |
| `investment_report_get` | `QueryService.get_bundle` | Returns the full bundle. |
| `investment_report_decide_item` | `DecisionService.record` | Idempotent; allows `partial_approve` with required payload snapshot. |
| `investment_report_activate_watch` | `WatchActivationService.activate` | Idempotent per source item. |
| `investment_report_context_get` | `QueryService.previous_report_context` | n_prior + filters. |

Each handler:
1. Validates input via the Plan 2 Pydantic request schema (`IngestReportRequest`, `RecordDecisionRequest`, `ActivateWatchRequest`).
2. Opens a fresh `AsyncSessionLocal()` per call, calls the service, commits, and returns the serialised response.
3. Returns `{"success": True, ...}` matching the legacy MCP convention.

---

## Tasks

### Task 1 — Response Pydantic models
Append to `app/schemas/investment_reports.py`: `InvestmentReportResponse`, `InvestmentReportItemResponse`, `InvestmentReportItemDecisionResponse`, `InvestmentWatchAlertResponse`, `InvestmentWatchEventResponse`, `InvestmentReportBundle`, `InvestmentReportListResponse`, `PreviousReportContextResponse`. Each uses `from_attributes=True` so `model_validate(orm_instance)` works.

### Task 2 — HTTP router
Create `app/routers/investment_reports.py` with the 3 GET routes from the table above. Service injected via `Depends(get_db)` → `InvestmentReportQueryService`. Auth via `get_authenticated_user`. 404 on missing report.

### Task 3 — MCP handlers
Create `app/mcp_server/tooling/investment_reports_handlers.py` with 6 `*_impl` functions and the `register_investment_report_tools(mcp)` registrar. Each impl validates the request via the Plan 2 schema, calls the service, commits, and returns a `{"success": True, ...}` dict.

### Task 4 — Wiring
- `app/main.py`: import the router and `include_router`.
- `app/mcp_server/tooling/registry.py`: import `register_investment_report_tools` and call it next to `register_analysis_report_tools` (legacy stays — clean cut is Plan 5).

### Task 5 — Router tests
`tests/test_investment_reports_router.py`:
- Build a minimal FastAPI app with the investment_reports router + auth override.
- Tests for list (empty + with filters), bundle (happy path + 404), context (with prior reports).
- Use the shared `session` fixture from the helpers plugin so DB state is seeded via the real services.

### Task 6 — MCP handler tests
`tests/test_investment_reports_mcp.py`:
- Call each `*_impl` directly (no FastMCP runtime needed for unit tests — that's the legacy pattern).
- Tests for create idempotency, list filter pass-through, get not-found, decide approve + status transition, activate watch, context retrieval.

### Task 7 — Final lint/typecheck/PR
- `ruff format` + `ruff check` clean on new + modified files.
- `ty check` clean for schemas + new modules.
- Full P1+P2+P3 sweep + legacy guard.
- Push `rob-265-plan-3`, open PR with base `rob-265-plan-2`.

---

## Out of scope (deferred to Plans 4/5)

- OpenClaw notification payload update (Plan 4 — wires the scanner that emits the events).
- Watch scanner re-wire onto `investment_watch_alerts` / `investment_watch_events` (Plan 4).
- Frontend `/invest/reports` views (Plan 5).
- Legacy clean cut — `analysis_*` and `watch_order_intent_ledger_*` removal (Plan 5).
- Sentry / observability instrumentation on the new tools (follow-up if needed).
