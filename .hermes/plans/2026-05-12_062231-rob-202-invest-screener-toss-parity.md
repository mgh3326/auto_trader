# ROB-202 Invest Screener Toss Parity Implementation Plan

> **For Hermes:** This is a read-only reconnaissance handoff for the next implementer. Implement with the auto-trader operations workflow and TDD. Do not mutate broker/order/watch/order-intent state.

**Goal:** Align `/invest/screener` `consecutive_gainers` (연속 상승세) with Toss observed behavior: `DAY_5 >= 0`, `주가_연속_상승 >= 5`, sorted by `C_주가등락률_1W DESC`, with a Toss-sized result set (~50-80 rows rather than 20).

**Architecture:** Keep the parity change scoped to the `/invest` view-model/read path first. The durable `invest_screener_snapshots` read model is already the right serving surface for consecutive gainers; adjust its view-model query and preset defaults, then add a read-only diagnostic script to compare auto_trader output with Toss exports/captures. Avoid schema/backfill/scheduler activation unless a later task explicitly approves it.

**Tech Stack:** FastAPI + SQLAlchemy async + Pydantic DTOs + React/Vite frontend + pytest.

---

## Current Context

Reconnaissance was read-only. No repo files were intentionally modified before this plan file.

Key findings:

- `/invest/api/screener/results` is implemented in `app/routers/invest_api.py:397-416` and accepts only `preset` and `market`; no explicit `limit`, `totalCount`, or pagination parameter exists.
- The response schema in `app/schemas/invest_screener.py:71-80` contains `results` but no `totalCount`, `limit`, or `hasMore`.
- The frontend `frontend/invest/src/api/screener.ts:13-20` sends only `preset` and `market`.
- The desktop page `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx:93-110` displays `results.results.length`; there is no frontend slicing to 20.
- Current preset mapping in `app/services/invest_view_model/screener_presets.py:94-103` already has:
  - `min_consecutive_up_days: 5`
  - `min_week_change_rate: 0.0`
  - `sort_by: "change_rate"`
  - `sort_order: "desc"`
  - `limit: 20`
- Snapshot-first consecutive-gainers logic lives in `app/services/invest_view_model/screener_service.py:108-182` and currently filters correctly for `consecutive_up_days >= 5` and `week_change_rate >= 0`, but orders by:
  1. `snapshot_date DESC`
  2. `consecutive_up_days DESC`
  3. `week_change_rate DESC NULLS LAST`
  4. `change_rate DESC NULLS LAST`
  This does not match Toss rank order, which should prioritize weekly change (`C_주가등락률_1W DESC`) among eligible rows.
- `_SNAPSHOT_FIRST_LIMIT = 20` in `app/services/invest_view_model/screener_service.py:46`, and the preset also sets `limit: 20`; this is the likely source of the observed count gap.
- Snapshot metrics are derived in `app/services/invest_screener_snapshots/builder.py` with `_LOOKBACK = 10`, `consecutive_up_days` from strict latest-backward increases, and `week_change_rate` from latest close vs `closes[-5]`.
- `kr_symbol_universe` currently has only `symbol`, `name`, `exchange`, `nxt_eligible`, `is_active`; no reliable `instrument_type` column is available for excluding ETFs/MMF/TDF/preferred stocks in this path.
- MCP screening supports `instrument_types` in `app/mcp_server/tooling/screening/common.py`, but that is a public tool contract. Do not casually narrow public defaults while fixing `/invest` Toss parity.
- Existing tests include:
  - `tests/test_invest_screener_week_change_filter.py`
  - `tests/test_invest_screener_presets.py`
  - `tests/test_invest_view_model_screener_service.py`
  - `tests/test_invest_screener_snapshots_repository.py`
  - `tests/test_invest_api_screener_router.py`
- Existing read-only coverage script: `scripts/diagnose_invest_screener_snapshots.py`.

Claude Code Opus delegation was attempted but unavailable: `claude` returned `Not logged in · Please run /login`.

---

## P0 Acceptance Criteria

1. For KR `consecutive_gainers`, the effective filters are:
   - `consecutive_up_days >= 5`
   - `week_change_rate >= 0`
2. Snapshot-first result rank matches Toss priority:
   - latest snapshot date remains selected/deduped safely,
   - within the served candidate set, rows are ordered by `week_change_rate DESC`, then stable tie-breakers.
3. Default `consecutive_gainers` result count is raised from 20 to a Toss-sized bounded limit (recommend 80) without changing every other preset.
4. Frontend result count reflects the larger backend result set; no frontend slicing or incorrect “20” assumptions remain.
5. A read-only diagnostic command can compare auto_trader `consecutive_gainers` output with a Toss symbol list/export and report missing/extra/rank deltas plus row metrics.
6. Unit/router tests cover filters, sort order, limit behavior, null handling, and response compatibility.
7. No broker/order/watch/order-intent/live trading mutation, no production DB writes, no backfill `--commit`, no scheduler activation, and no secret logging.

---

## Non-Common Stock Filtering Decision

Do not make ETF/MMF/TDF/preferred/common-stock exclusion a P0 schema/backfill change.

Reasoning:

- `kr_symbol_universe` has no instrument classification field today.
- `invest_screener_snapshots` also does not carry a reliable instrument type.
- MCP screening has an `instrument_types` concept, but changing public tool defaults risks unrelated contract changes.
- Name/suffix heuristics would create false positives/false negatives and make Toss parity look better without a trustworthy data source.

P0 should document and surface this as a known gap in the diagnostic report. P1 can add a proper `instrument_type` source/field/backfill if Toss diffs show non-common products are a material mismatch.

---

## Implementation Plan

### Task 1: Add focused tests for preset limit and Toss sort intent

**Objective:** Lock the desired `consecutive_gainers` defaults before changing implementation.

**Files:**
- Modify: `tests/test_invest_screener_presets.py`
- Modify: `tests/test_invest_screener_week_change_filter.py`

**Steps:**

1. Add/extend a test asserting `screening_filters_for("consecutive_gainers", market="kr")` returns:
   - `min_consecutive_up_days == 5`
   - `min_week_change_rate == 0.0`
   - `sort_by == "week_change_rate"`
   - `sort_order == "desc"`
   - `limit == 80` (or a named constant if introduced)
2. Keep the existing US assertions if the same logical preset should stay supported for US, but make sure the test wording says this is logical parity, not proven Toss US parity.
3. Run:
   - `uv run pytest tests/test_invest_screener_presets.py tests/test_invest_screener_week_change_filter.py -m unit -q`
4. Expected before implementation: failures on `sort_by` and `limit`.

### Task 2: Raise only the consecutive-gainers default limit

**Objective:** Return Toss-sized results for this preset without changing all other screener presets.

**Files:**
- Modify: `app/services/invest_view_model/screener_presets.py:94-103`
- Modify: `app/services/invest_view_model/screener_service.py:46` if keeping `_SNAPSHOT_FIRST_LIMIT` aligned

**Recommended change:**

- Introduce a named constant near the preset filters, e.g. `CONSECUTIVE_GAINERS_LIMIT = 80`, or keep a local literal with a comment.
- Change only the `consecutive_gainers` filter from `limit: 20` to `limit: 80`.
- Change `sort_by` from `"change_rate"` to `"week_change_rate"` so fallback `ScreenerService.list_screening(**filters)` and tests describe the same desired behavior.
- Consider changing `_SNAPSHOT_FIRST_LIMIT` from 20 to 80 or adding `_CONSECUTIVE_GAINERS_LIMIT = 80`; avoid raising default limits for unrelated paths.

**Caution:** `build_screener_results()` calls `_load_consecutive_gainers_from_snapshots(... limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT))`, so the preset limit already controls snapshot-first behavior. A separate `_SNAPSHOT_FIRST_LIMIT` change is mostly fallback/clarity unless callers omit filters.

### Task 3: Fix snapshot-first rank ordering to weekly change

**Objective:** Match Toss ranking (`C_주가등락률_1W DESC`) after applying the two Toss filters.

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py:108-182`
- Test: `tests/test_invest_view_model_screener_service.py`

**Recommended query behavior:**

- Keep filters:
  - `InvestScreenerSnapshot.market == market`
  - `InvestScreenerSnapshot.consecutive_up_days >= 5`
  - `InvestScreenerSnapshot.week_change_rate >= 0`
- Change ordering so weekly change outranks streak length:
  - Prefer latest snapshot rows, but avoid letting stale rows for one date outrank fresh rows incorrectly.
  - Recommended P0 simple ordering:
    1. `InvestScreenerSnapshot.snapshot_date.desc()`
    2. `InvestScreenerSnapshot.week_change_rate.desc().nullslast()`
    3. `InvestScreenerSnapshot.consecutive_up_days.desc().nullslast()`
    4. `InvestScreenerSnapshot.change_rate.desc().nullslast()`
    5. `InvestScreenerSnapshot.symbol.asc()`
- Keep overfetch/dedup (`limit * 4`) if historical duplicate dates exist, but add a test to ensure the final list order is by weekly change among returned latest rows.

**Test shape:**

Use the existing `_FakeSnapshot`, `_FakeSession`, and `_FakeExecuteResult` helpers in `tests/test_invest_view_model_screener_service.py` if possible. Add a test where snapshots have:

- A: `consecutive_up_days=9`, `week_change_rate=1.0`
- B: `consecutive_up_days=5`, `week_change_rate=8.0`
- C: `consecutive_up_days=6`, `week_change_rate=3.0`

Expected result order should be B, C, A.

If the fake session cannot inspect SQL ordering, add an integration-style repository/service test with `db_session` and real inserted snapshot rows.

### Task 4: Ensure fallback screening path can sort by week_change_rate

**Objective:** Avoid divergence when snapshot-first has no rows and `screening_service.list_screening(**filters)` is used.

**Files:**
- Inspect/modify: `app/mcp_server/tooling/screening/common.py`
- Inspect/modify: the implementation behind `ScreenerService.list_screening()` and `screen_stocks_unified()` (search for `sort_by` handling)
- Test: `tests/test_invest_screener_week_change_filter.py` or a dedicated screening test

**Steps:**

1. Verify that `sort_by="week_change_rate"` is accepted and actually sorts on row field `week_change_rate`.
2. If unsupported, add a narrow sort-key mapping for `week_change_rate` without changing public defaults.
3. Keep `min_week_change_rate` filtering already tested in `tests/test_invest_screener_week_change_filter.py`.
4. Add a test where fallback rows with week changes 1.0, 8.0, 3.0 are sorted 8.0, 3.0, 1.0 when requested.

**Caution:** This path may involve MCP/public tool behavior. Keep new support additive; do not remove supported sort keys or alter unrelated default sort behavior.

### Task 5: Decide whether to expose totalCount/limit in the API

**Objective:** Avoid unnecessary schema churn while making the UI understandable.

**Recommendation for P0:** Do not add `totalCount`, `limit`, or pagination fields yet unless the implementer confirms a product need.

Reasoning:

- The frontend already displays `results.results.length` at `DesktopScreenerPage.tsx:99`.
- The response schema currently forbids extra fields; adding fields is additive for TS only if all generated/manual types and tests are updated.
- Toss parity complaint is likely caused by the backend cap of 20, not a frontend hidden count.

If product wants “80개 중 20개” or load-more behavior, implement as a separate P1 API contract change:

- Add optional `limit` query param to `app/routers/invest_api.py:397-405`.
- Add `totalCount`, `limit`, `hasMore` to `app/schemas/invest_screener.py` and `frontend/invest/src/types/screener.ts`.
- Update `fetchScreenerResults()` to pass limit explicitly.

### Task 6: Add a read-only Toss parity diagnostic script

**Objective:** Give K2/operator a repeatable way to quantify parity gaps without DB writes or Toss/browser secrets in logs.

**Files:**
- Prefer extend: `scripts/diagnose_invest_screener_snapshots.py`
- Or create: `scripts/diagnose_invest_screener_toss_parity.py`
- Test: a small unit test for pure diff logic, e.g. `tests/test_invest_screener_toss_parity_diagnostics.py`

**Recommended CLI:**

- `uv run python -m scripts.diagnose_invest_screener_toss_parity --market kr --preset consecutive_gainers --toss-symbols-file /path/to/toss_symbols.csv --limit 80`
- Input CSV/JSON should accept at least `symbol`, and optionally `rank`, `name`, `week_change_rate`, `consecutive_up_days` if Toss export/capture has them.
- The script must be read-only. It may query `invest_screener_snapshots` and/or call the local view-model service, but must not write to DB.
- Output should include:
  - auto_trader count
  - Toss count
  - overlap count
  - missing-from-auto_trader symbols
  - extra-in-auto_trader symbols
  - top rank deltas
  - for each diff row, auto_trader `week_change_rate`, `consecutive_up_days`, `snapshot_date`, `_screener_snapshot_state` if available
  - a note that non-common-stock filtering is not P0 unless instrument type is present

**Safety requirements:**

- Do not accept or print Toss cookies/headers/tokens.
- If an input value looks like a cookie/header/token, redact as `[REDACTED]` or reject.
- Do not fetch Toss live from the script in P0; consume an operator-provided symbol export/list.

### Task 7: Frontend verification and no-op UI changes

**Objective:** Confirm the frontend naturally renders the larger result set and count.

**Files:**
- Inspect/test: `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx`
- Inspect/test: `frontend/invest/src/desktop/screener/ScreenerFilterBar*`
- Inspect/test: `frontend/invest/src/desktop/screener/ScreenerResultsTable*`
- Types: `frontend/invest/src/types/screener.ts`

**Expected P0 result:** likely no frontend code change is needed if response still only has `results` and table can render 80 rows.

**Tests/checks:**

- Add/update a frontend test only if there is an existing test suite around screener components.
- Ensure no component slices `rows.slice(0, 20)` or hardcodes 20. Use search before changing.
- Run:
  - `cd frontend/invest && npm run typecheck`
  - `cd frontend/invest && npm test -- --run` if test suite is configured and not too broad
  - `cd frontend/invest && npm run build`

### Task 8: Run targeted backend validation

**Objective:** Prove the parity behavior without live or mutating operations.

Recommended commands from repo root:

```bash
uv run pytest \
  tests/test_invest_screener_presets.py \
  tests/test_invest_screener_week_change_filter.py \
  tests/test_invest_view_model_screener_service.py \
  tests/test_invest_api_screener_router.py \
  -m unit -q
```

If repository tests require DB fixtures for snapshot ordering:

```bash
uv run pytest tests/test_invest_screener_snapshots_repository.py tests/test_invest_view_model_screener_service.py -q
```

Do not run tests marked `live` unless explicitly approved with the repository's `--run-live` convention.

### Task 9: Optional read-only smoke after implementation

**Objective:** Confirm the local read path returns Toss-sized rows and no provider/internal leakage.

Options:

- Unit/service-level smoke with fake rows is enough for PR stage.
- If a local dev server is already running, call authenticated `/invest/api/screener/results?preset=consecutive_gainers&market=kr` and verify:
  - row count can exceed 20 when snapshot table has enough rows,
  - `freshness.dataState` is meaningful,
  - warnings do not expose provider hostnames, credentials, or internals.

Do not run production backfills, scheduler activation, or any command with `--commit` in this task.

---

## Files Likely to Change

P0 likely changes:

- `app/services/invest_view_model/screener_presets.py`
  - `consecutive_gainers` `sort_by` -> `week_change_rate`
  - `consecutive_gainers` `limit` -> 80
- `app/services/invest_view_model/screener_service.py`
  - snapshot-first ordering -> weekly change first after snapshot freshness/date
  - maybe named limit constant
- `app/mcp_server/tooling/screening/common.py` or related screening implementation
  - only if fallback sorting does not support `week_change_rate`
- `scripts/diagnose_invest_screener_toss_parity.py` or `scripts/diagnose_invest_screener_snapshots.py`
  - read-only parity report
- `tests/test_invest_screener_presets.py`
- `tests/test_invest_screener_week_change_filter.py`
- `tests/test_invest_view_model_screener_service.py`
- possibly `tests/test_invest_api_screener_router.py`
- possibly `frontend/invest/src/types/screener.ts` only if API response schema changes (not recommended for P0)

P1/P2 likely changes:

- Add reliable KR instrument classification source/field.
- Backfill `instrument_type` for KR universe/snapshots after explicit approval.
- Add `totalCount`/pagination/load-more API and UI if product requires it.

---

## Risks and Open Questions

1. **DAY_5 exact semantics:** `builder.py` currently computes `week_change_rate` using `closes[-5]`. Verify whether this matches Toss `DAY_5` exactly before changing metric derivation. Changing derivation may require snapshot rebuild/backfill and should not be bundled into P0 without evidence.
2. **Stale vs latest ordering:** If the table contains multiple snapshot dates, `snapshot_date DESC` first is safer for freshness but can still interact with rank parity. The diagnostic should report snapshot dates so this is visible.
3. **Fallback path support:** `sort_by="week_change_rate"` must be validated in the actual `ScreenerService.list_screening()` path; otherwise snapshot-empty behavior may remain Toss-divergent.
4. **ETF/MMF/TDF/preferred exclusion:** Important for exact Toss parity, but not safe as P0 without reliable instrument classification.
5. **Performance:** Increasing default limit to 80 is bounded, but snapshot query overfetch currently uses `limit * 4`; with 80 this is 320 rows, likely acceptable. If slow, add an index only after measuring and with a migration plan.
6. **Schema churn:** Adding count/pagination fields is additive but touches backend schema, frontend types, tests, and compatibility. Avoid unless necessary.

---

## Suggested K2 Handoff

Implement P0 in this order:

1. Tests for preset defaults and snapshot ordering.
2. Preset limit/sort change.
3. Snapshot-first order change.
4. Fallback `week_change_rate` sort support if missing.
5. Read-only Toss parity diagnostic script.
6. Targeted backend tests, frontend typecheck/build if frontend touched.

Safety boundaries for K2:

- No live/paper broker orders.
- No watch/order-intent mutation.
- No production DB writes.
- No backfill `--commit`.
- No scheduler activation.
- No secrets/cookies/credentials in output.
