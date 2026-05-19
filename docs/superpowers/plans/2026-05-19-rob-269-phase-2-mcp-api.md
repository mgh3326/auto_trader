# ROB-269 Phase 2 — MCP/API Surface (Short Plan)

> Phase 2 of ROB-269. Builds on Phase 1 (`docs/superpowers/plans/2026-05-19-rob-269-phase-1-foundation.md`). PR base: `rob-269` (stacked draft until PR 874 merges, then retarget to `main`).
>
> **Branch:** `rob-269-phase2-mcp-api` (off `origin/rob-269`)
> **Worktree:** `/Users/mgh3326/work/auto_trader.rob-269-phase2-mcp-api`
> **Goal:** Expose Phase 1 foundation through a feature-flagged MCP + HTTP read/audit surface. No report generator, no live collectors, no broker mutation.

---

## 1. MCP/API contract

**4 MCP tools** registered via `app/mcp_server/tooling/investment_snapshots_registration.py`. All read-only with respect to broker/order/watch state; only write paths are append-only into `review.investment_snapshot_*` tables.

| Tool | Request | Response | Side effects |
|---|---|---|---|
| `investment_snapshot_bundle_ensure` | `purpose`, `market`, `account_scope?`, `policy_version`, `mode ∈ {ensure_fresh, reuse_only}`, `symbols?`, `candidate_limit?`, `manual_snapshots?` (optional pre-collected payloads, keyed by `snapshot_kind`) | `{bundle_uuid, status ∈ {complete, partial, stale_fallback, failed, reused}, created: bool, coverage_summary, freshness_summary, missing_sources, warnings}` | INSERT into runs/snapshots/bundles/bundle_items only when no fresh bundle exists. In `reuse_only`, never collects — returns `failed` if no fresh bundle. |
| `investment_snapshot_bundle_get` | `bundle_uuid` | `{bundle: {…header…}, items: [{snapshot_uuid, role, snapshot_kind, freshness_status, source_kind, source_uri?, as_of}]}`. `include_payload_preview: bool` (default `False`) controls whether `payload_json` is partially echoed. | None. |
| `investment_snapshot_list` | `market?`, `symbol?`, `snapshot_kind?`, `source_kind?`, `freshness_status?`, `since?`, `limit ≤ 100` | `{snapshots: [{snapshot_uuid, snapshot_kind, market, symbol, as_of, freshness_status, source_kind, source_uri?}]}`. No `payload_json` in list view. | None. |
| `investment_snapshot_refresh_request` | `reason`, `purpose`, `market`, `account_scope?`, `symbols?`, `snapshot_kinds?` | `{run_uuid, status: 'running'}` | INSERT one `investment_snapshot_runs` row with `purpose='manual_refresh'` and `requested_by='reviewer'` or `'user'` based on caller identity. **Does not collect.** Phase 3 schedulers will discover the row and act on it. |

**REST router** `app/routers/investment_snapshots.py`, prefix `/trading/api/investment-snapshots`, **GET-only**:

- `GET /bundles/{bundle_uuid}` — mirrors `investment_snapshot_bundle_get` (no payload preview at this surface — kept for MCP).
- `GET /bundles` — mirrors `investment_snapshot_list` for bundles (filters: `purpose`, `market`, `account_scope`, `status`, `limit`).
- `GET /snapshots` — mirrors `investment_snapshot_list`.
- **No POST/PUT/DELETE.** Refresh-request is MCP-only in Phase 2 (no HTTP write path) so the HTTP surface is provably read-only.

Auth: GET endpoints use existing `Depends(get_authenticated_user)`. MCP tools rely on existing `caller_identity_middleware`.

## 2. Feature flag behavior

`INVESTMENT_SNAPSHOTS_MCP_ENABLED: bool = False` in `app/core/config.py` (mirrors `RESEARCH_REPORTS_INGEST_COMMIT_ENABLED`, `EXECUTION_LEDGER_COMMIT_ENABLED` pattern).

- **`False` (default):**
  - `register_investment_snapshots_tools(mcp)` is **not** called inside `register_all_tools` → 4 MCP tools absent.
  - Router is **not** mounted on the FastAPI app → all 3 GET endpoints return 404.
  - Code paths still import cleanly; tests can override the flag.
- **`True`:**
  - 4 MCP tools register.
  - Router mounts.
- The same flag gates both surfaces; no separate MCP/router flag. Prevents the "MCP exposed but HTTP hidden" mismatch.

Activation order per pre-plan §5: this flag flips after the PR 2 merge, before Phase 3 work starts.

## 3. Service / repository call graph

```
MCP tool / GET endpoint
  │
  ├──▶ SnapshotBundleEnsureService.ensure(request)
  │     ├─ repo.find_latest_bundle(purpose, market, account_scope, policy_version)
  │     ├─ classify_freshness(bundle.as_of, now, policy.bundle_ttl)
  │     ├─ if fresh: return reused                  ◀── no new run
  │     ├─ if reuse_only: return failed             ◀── no collection
  │     ├─ repo.insert_run(...)
  │     ├─ for kind in policy.required_kinds:
  │     │    payload = manual_snapshots[kind]                   ── if caller-supplied
  │     │             or collectors.get(kind).collect(...)      ── if registered (Phase 3+)
  │     │             or None                                    ── results in 'unavailable'
  │     │    repo.insert_snapshot(...)
  │     ├─ same for optional_kinds (with bounded timeout, errors → 'unavailable')
  │     ├─ derive bundle status from required vs optional outcomes
  │     ├─ repo.insert_bundle(...) + repo.link_bundle_item(...) per snapshot
  │     └─ return response DTO
  │
  ├──▶ SnapshotBundleReadService.get_bundle(bundle_uuid)
  │     ├─ repo.get_bundle_by_uuid + repo.list_bundle_items_with_snapshots
  │     └─ DTO transform
  │
  ├──▶ SnapshotBundleReadService.list_bundles / list_snapshots (filtered)
  │     ├─ repo.list_bundles / list_snapshots (new read methods)
  │     └─ DTO transform
  │
  └──▶ SnapshotRefreshRequestService.record(request)
        └─ repo.insert_run(purpose='manual_refresh', ...) — single INSERT
```

`SnapshotCollectorRegistry` is a runtime map of `snapshot_kind → SnapshotCollectorProtocol`. **Empty by default in Phase 2.** Tests register fakes. Phase 3 will register production collectors (KIS / journal / market / news). Ensures Phase 2 has zero live HTTP surface.

New read methods on `InvestmentSnapshotsRepository` (does **not** widen the append-only contract — these are SELECT-only):
- `find_latest_bundle(purpose, market, account_scope, policy_version) -> Bundle | None`
- `get_bundle_by_uuid(bundle_uuid) -> Bundle | None`
- `list_bundle_items_with_snapshots(bundle_id) -> list[(item, snapshot)]`
- `list_bundles(filters) -> list[Bundle]`
- `list_snapshots(filters) -> list[Snapshot]`

`test_append_only.py` surface lock list **must be updated** to include these read methods. Adding a new mutation prefix (`update_/delete_/...`) is still rejected.

## 4. Snapshot write boundary / prohibited mutation boundary

**Allowed writes** (snapshot domain only, append-only):
- INSERT into `review.investment_snapshot_runs`
- INSERT into `review.investment_snapshots`
- INSERT into `review.investment_snapshot_bundles`
- INSERT into `review.investment_snapshot_bundle_items`

**Prohibited writes** (verified by grep guard test):
- No INSERT/UPDATE/DELETE against any non-snapshot table from new service modules.
- No imports of `KISTradingService`, `OrderExecutionService`, `AlpacaPaperOrdersService`, `WatchActivationService`, broker MCP handlers.
- No `httpx`/`aiohttp`/`requests`/`urllib.request` in the new service modules (Phase 2 has no live HTTP at all; even GETs against external systems are deferred to Phase 3 collectors).
- No `cancel_*` / `submit_*` / `modify_*` method calls anywhere in new code.

**MCP tool surface invariants:**
- `investment_snapshot_bundle_ensure` is the **only** snapshot-table write path. The other 3 tools are pure reads (or `refresh_request` which is a single INSERT into runs).
- No tool writes outside `review.investment_snapshot_*`.

## 5. Tests

### Service / repository
- `test_bundle_ensure_service.py` — 8 cases:
  - reuse_only with no fresh bundle → `failed`
  - reuse_only with fresh bundle → `reused`, no new run
  - ensure_fresh with empty collectors + no manual payloads → `failed` (no data)
  - ensure_fresh with manual payloads for all required kinds → `complete`
  - ensure_fresh with manual payloads for required + 1 optional failing → `partial`
  - ensure_fresh with stale_fallback (hard-stale required, no fresh source) → `stale_fallback`
  - ensure_fresh second call within soft TTL → bundle reused, no second run
  - ensure_fresh respects `policy_snapshot_json` freeze on the run

- `test_collectors.py` — registry register/lookup/missing-kind error, protocol-shape conformance test (Pydantic SnapshotCollectResult).

- `test_read_service.py` — get_bundle 404, get_bundle full round trip incl. items + snapshots, list filters (market/symbol/kind), `limit` clamp at 100.

- `test_refresh_request_service.py` — INSERTs one run row with the right `purpose`+`requested_by`, returns `run_uuid`.

- `test_append_only.py` — extends Phase 1 surface lock to include new read methods (mutation prefix rule unchanged).

### MCP / router
- `test_investment_snapshots_tools.py` — 4 tools each: input validation, happy path against in-memory DB, error mapping (404, validation).
- `test_investment_snapshots_router.py` — 3 GET endpoints: 200, 404, filter behavior, no POST/PUT/DELETE handlers exist.
- `test_investment_snapshots_feature_flag.py` — 4 cases:
  - flag False → MCP registration call skipped, 4 tool names absent from server
  - flag False → router not mounted, GET returns 404 at FastAPI level
  - flag True → 4 tools present
  - flag True → router mounted

### Safety boundary
- `test_mutation_boundary.py` — static grep guard:
  - new service modules import no `httpx/aiohttp/requests/urllib.request`
  - new service modules import no broker/order/watch-intent service classes (allowlist of forbidden names)
  - new modules contain no `INSERT/UPDATE/DELETE` against non-`review.investment_snapshot_*` tables (SQL string scan, conservative — falses are OK to bypass with `# noqa: rob-269-boundary`)

### Validation (mostly inherited from Phase 1)
- Existing `SnapshotCreate` validators (source_ref triple + domain_ref) re-exercised through `ensure_bundle` happy paths.
- New `SnapshotBundleEnsureRequest` DTO validation: `mode` enum, `policy_version` required, `symbols ⊆ kr/us/crypto market constraints`.

**Test count target:** ~30 new tests on top of Phase 1's 25.

## 6. Expected files

**Create (15 new):**
- `app/services/action_report/common/snapshot_bundle.py` — `SnapshotBundleEnsureService`
- `app/services/investment_snapshots/collectors.py` — protocol + registry + result DTO
- `app/services/investment_snapshots/read_service.py` — `SnapshotBundleReadService`
- `app/services/investment_snapshots/refresh_request_service.py` — `SnapshotRefreshRequestService`
- `app/services/investment_snapshots/policy.py` — `intraday_action_report_v1` constants (per-kind TTLs from pre-plan §3-defaults)
- `app/schemas/investment_snapshots_mcp.py` — request/response DTOs for the 4 tools + 3 endpoints
- `app/mcp_server/tooling/investment_snapshots_tools.py` — 4 tool function implementations
- `app/mcp_server/tooling/investment_snapshots_registration.py` — registration + `INVESTMENT_SNAPSHOTS_TOOL_NAMES`
- `app/routers/investment_snapshots.py` — 3 GET endpoints
- `tests/services/investment_snapshots/test_bundle_ensure_service.py`
- `tests/services/investment_snapshots/test_collectors.py`
- `tests/services/investment_snapshots/test_read_service.py`
- `tests/services/investment_snapshots/test_refresh_request_service.py`
- `tests/services/investment_snapshots/test_mutation_boundary.py`
- `tests/test_investment_snapshots_feature_flag.py` (or `tests/mcp_server/` + `tests/routers/` if split is cleaner)

**Modify (4):**
- `app/core/config.py` — add `INVESTMENT_SNAPSHOTS_MCP_ENABLED: bool = False`
- `app/mcp_server/tooling/registry.py` — flag-gated `register_investment_snapshots_tools(mcp)` call
- `app/mcp_server/tooling/__init__.py` — lazy export entries
- `app/main.py` (or wherever routers are mounted — verify on first commit) — flag-gated router include
- `app/services/investment_snapshots/repository.py` — add 5 SELECT-only methods (no mutation prefix)
- `tests/services/investment_snapshots/test_append_only.py` — surface-lock list updated for the new read methods

## 7. Non-goals (explicit out-of-scope for Phase 2)

- ❌ Report generator integration (`investment_report_create` does not call `ensure_snapshot_bundle` in this PR)
- ❌ KR action report cutover (no LLM/report-builder code touched)
- ❌ `us_action_report/` → `action_report/<market>/` lift beyond defining `action_report/common/` and the single `snapshot_bundle.py` file inside it (no `us_action_report/` files moved, no renames)
- ❌ `/invest` frontend provenance rendering (no React/SPA changes)
- ❌ Prefect / TaskIQ scheduler enablement (no flow files, no recurring jobs)
- ❌ Production deploy / unpause checklists (this PR is internal-surface only, default-off)
- ❌ Real broker / order / watch-intent mutation (banned by safety boundary §4)
- ❌ Live KIS / Upbit / Alpaca HTTP collectors (Phase 3 work; Phase 2 collectors registry is empty in prod, populated only by tests with fakes)
- ❌ Caller identity / authz changes (use existing `caller_identity_middleware` and `get_authenticated_user`)
- ❌ Snapshot dedup semantic re-design — Phase 1 §3b-1 decision stands; reviewer-pass note remains the contract

---

## Implementation order (TDD red-green-commit per step)

Each step ends with a focused commit. Commits stay local until the end; final push as one branch.

1. `feat(rob-269-p2): INVESTMENT_SNAPSHOTS_MCP_ENABLED flag in settings` — config.py only + 1 settings test.
2. `feat(rob-269-p2): snapshot policy constants (intraday_action_report_v1)` — `policy.py` + tests.
3. `feat(rob-269-p2): snapshot collector protocol + registry` — `collectors.py` + tests.
4. `feat(rob-269-p2): request/response DTOs for snapshot MCP surface` — `investment_snapshots_mcp.py` + tests.
5. `feat(rob-269-p2): repository SELECT-only read methods` — repository.py extension + tests (append-only surface lock updated).
6. `feat(rob-269-p2): SnapshotBundleReadService` — `read_service.py` + 4-test slice.
7. `feat(rob-269-p2): SnapshotRefreshRequestService` — `refresh_request_service.py` + 2-test slice.
8. `feat(rob-269-p2): SnapshotBundleEnsureService` — `snapshot_bundle.py` + 8-test slice (the core).
9. `feat(rob-269-p2): MCP tool functions + registration (flag-gated)` — tools.py + registration.py + registry.py wiring + flag tests.
10. `feat(rob-269-p2): HTTP router (flag-gated, GET-only)` — `routers/investment_snapshots.py` + main mount + router tests + flag tests.
11. `test(rob-269-p2): mutation boundary grep guard` — `test_mutation_boundary.py`.
12. `chore(rob-269-p2): ruff cleanup` if needed.

## Handoff format (at end)

- Branch: `rob-269-phase2-mcp-api`
- Commits: list with hashes + one-line subjects.
- Migration: none (Phase 2 is code-only; reuses Phase 1 schema).
- Tests: pasted `pytest` summary line(s).
- Lint: clean or list of fixes.
- Local commits made? Yes, no push until final.
- Phase 1 dependency check: does this branch still rebase clean onto `origin/rob-269`?
- Open `# TODO(rob-269 reviewer):` notes: file + line if any.

After implementation, push branch + create stacked draft PR with base `rob-269`. Do not merge anything; wait for PR 874 to land first per user's workflow.
