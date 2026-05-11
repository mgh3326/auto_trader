# ROB-203 /invest Coverage Actionability + All-Market Symbol Drilldown Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. This planner task is read-only except for this plan artifact.

**Goal:** Make `/invest/coverage` and `/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT` actionable by returning symbol-level diagnostics across KR/US, exposing coverage-to-work-queue metadata, rendering that metadata in the UI, and documenting approval gates.

**Architecture:** Extend the existing ROB-201 coverage contract rather than introducing a new coverage system. Keep the endpoint read-only: it inspects only local `auto_trader` DB/read-model state, labels Toss/Naver as reference/candidate signals, and maps coverage states to recommended work-queue actions without triggering backfills, schedulers, broker calls, watch/order intents, or production DB writes.

**Tech Stack:** FastAPI + Pydantic v2 schemas, SQLAlchemy async sessions, pytest/httpx backend tests, React/TypeScript frontend under `frontend/invest`, Vitest/React Testing Library for targeted frontend tests, markdown docs.

---

## Current Context and Important Findings

1. The active worktree is:
   `/Users/mgh3326/worktrees/auto_trader/rob-203-coverage-actionability`

2. Current branch is:
   `feature/rob-203-coverage-actionability`

3. The active branch currently appears to be based on `origin/main` and does **not** contain the ROB-201 invest coverage implementation. Searches in this worktree found no `app/schemas/invest_coverage.py`, no `app/services/invest_coverage_service.py`, and no frontend coverage page/API files.

4. The existing coverage implementation is on:
   `origin/kanban/ROB-201-naver-coverage`

5. ROB-201 files inspected from that branch:
   - `app/schemas/invest_coverage.py`
   - `app/services/invest_coverage_service.py`
   - `app/routers/invest_api.py`
   - `tests/test_invest_coverage.py`
   - `frontend/invest/src/api/coverage.ts`
   - `frontend/invest/src/types/coverage.ts`
   - `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`
   - `frontend/invest/src/__tests__/routes.test.tsx`
   - `docs/invest-coverage-dashboard.md`
   - `docs/superpowers/plans/2026-05-11-rob-201-naver-coverage-contract.md`

6. Primary discovered backend bug in ROB-201 code:
   `build_invest_coverage(..., market="all", symbols=[...])` accepts `market="all"`, but `_symbol_rows(db, market, symbols, trading_day)` starts with:

   ```python
   if not symbols or market not in {"kr", "us"}:
       return []
   ```

   Therefore `/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT` returns no symbol rows even though the top-level market contract supports `all`.

7. Claude Code Opus delegation was attempted but unavailable in this runtime:
   - Command: `claude -p --model opus --permission-mode plan --max-turns 20 < /tmp/rob-203-planner-prompt.md`
   - Result: exit code 1, `Not logged in · Please run /login`
   - Actual planner model for this handoff: Hermes `planner` profile using the current CLI model, not Claude Code Opus.

---

## Safety and Approval Gates

Keep these gates explicit in implementation, tests, docs, Linear comments, and review handoff:

1. Read-only only for this issue:
   - No production DB writes.
   - No backfills.
   - No scheduler/TaskIQ/Prefect activation.
   - No broker/KIS/Upbit/Alpaca calls.
   - No order submit/cancel/modify.
   - No watch-alert or order-intent mutations.
   - No request-path scraping.

2. Source-of-truth boundaries:
   - `auto_trader` DB/read models remain source of truth.
   - KIS/Upbit/news-ingestor owned ingest paths remain source-of-truth inputs where already designed.
   - Toss is a parity/reference benchmark only.
   - Naver remains candidate/reference only; never set `sourceOfTruth` to `naver_finance`, `naver_research`, or Naver discussion data.

3. Any future production remediation from coverage output must be separately approved:
   - Bounded data backfill approval.
   - Scheduler activation approval.
   - Broker/order approval if a future unrelated workflow reaches trading execution.

---

## Branch Integration Strategy

### Task 1: Bring ROB-201 coverage baseline into the ROB-203 worktree

**Objective:** Make the ROB-203 branch contain the already planned/implemented ROB-201 coverage contract before applying ROB-203 deltas.

**Files expected from ROB-201 baseline:**
- Create/modify: `app/schemas/invest_coverage.py`
- Create/modify: `app/services/invest_coverage_service.py`
- Modify: `app/routers/invest_api.py`
- Create/modify: `tests/test_invest_coverage.py`
- Create/modify: `frontend/invest/src/api/coverage.ts`
- Create/modify: `frontend/invest/src/types/coverage.ts`
- Create/modify: `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`
- Modify: frontend routing/navigation files that register `/invest/coverage` from ROB-201
- Create/modify: `docs/invest-coverage-dashboard.md`

**Implementation guidance:**
- Prefer merging/cherry-picking the ROB-201 branch or rebasing ROB-203 on a main commit that already includes ROB-201, if available.
- If ROB-201 is not merged, explicitly apply the ROB-201 coverage files first, then commit as a baseline before ROB-203-specific changes.
- Do not silently reimplement ROB-201 from memory; preserve the schema and Naver candidate semantics already reviewed in ROB-201.

**Verification:**

Run after baseline is present:

```bash
pytest tests/test_invest_coverage.py -q
```

Expected before ROB-203 changes: ROB-201 tests pass, but there should be no coverage yet for `market=all&symbols=005930,AAPL,MSFT`.

---

## Backend Contract Plan

### Task 2: Add actionability metadata to coverage schemas

**Objective:** Make coverage output directly mappable to a safe work queue without executing work.

**File:**
- Modify: `app/schemas/invest_coverage.py`

**Add narrow, explicit metadata fields. Suggested schema additions:**

```python
CoverageActionPriority = Literal["none", "low", "medium", "high", "blocked"]
CoverageActionKind = Literal[
    "none",
    "monitor",
    "investigate",
    "repair_read_model",
    "backfill_candidate",
    "scheduler_candidate",
    "provider_contract_needed",
    "unsupported_no_action",
]
CoverageApprovalGate = Literal[
    "none",
    "code_review",
    "production_db_write_approval",
    "scheduler_activation_approval",
    "broker_order_approval",
]

class CoverageActionability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: CoverageActionPriority = "none"
    action: CoverageActionKind = "none"
    queue: str | None = None
    approvalGates: list[CoverageApprovalGate] = Field(default_factory=list)
    reason: str | None = None
    safeByDefault: bool = True
```

Then attach it to both surface-level and symbol-level diagnostics:

```python
class InvestCoverageSurface(BaseModel):
    ...
    actionability: CoverageActionability = Field(default_factory=CoverageActionability)

class InvestCoverageSymbol(BaseModel):
    ...
    actionability: CoverageActionability = Field(default_factory=CoverageActionability)
```

**Design rules:**
- `fresh` -> `priority="none"`, `action="monitor"` or `none`, no gates.
- `partial`/`stale` -> usually `priority="medium"`, `action="repair_read_model"` or `backfill_candidate"`, gate `production_db_write_approval` if remediation would write production rows.
- `missing` -> usually `priority="high"`, `action="backfill_candidate"` for existing read models or `provider_contract_needed` if the durable model is not defined.
- `provider_unwired` -> `priority="blocked"`, `action="provider_contract_needed"`, gate `code_review`; do not imply immediate backfill.
- `unsupported` -> `priority="none"`, `action="unsupported_no_action"`, no remediation unless product scope changes.
- `error` -> `priority="high"`, `action="investigate"`, gate depends on the concrete surface.

**Testing:**
- Add Pydantic schema tests in `tests/test_invest_coverage.py` that instantiate `InvestCoverageSurface` and `InvestCoverageSymbol` with actionability.
- Assert `extra="forbid"` still catches unexpected fields if there is an existing style for that in tests.

---

### Task 3: Add a helper to derive actionability from surface state

**Objective:** Centralize state-to-queue mapping so backend, tests, UI, and docs stay consistent.

**File:**
- Modify: `app/services/invest_coverage_service.py`

**Suggested helper:**

```python
def _actionability_for_surface(
    *,
    surface: str,
    state: CoverageState,
    market: str | None,
    source_of_truth: str,
) -> CoverageActionability:
    ...
```

**Behavior:**
- Return an explicit `CoverageActionability` for every `InvestCoverageSurface`.
- Include a `queue` string such as:
  - `invest-data-read-models`
  - `news-ingestor`
  - `market-events-ingestion`
  - `research-report-ingestion`
  - `provider-contract`
  - `none`
- Include `approvalGates` only for actions that require approval before execution.
- Never perform the action from this helper.

**Recommended queue mapping:**
- `symbol_universe` -> `invest-data-read-models`
- `screener_snapshots` -> `invest-screener-snapshots`
- `news_feed` -> `news-ingestor`
- `calendar_events` -> `market-events-ingestion`
- `research_reports` -> `research-report-ingestion`
- `investor_flow` -> `investor-flow-ingestion`
- `holdings` -> `account-panel-read-model`
- `pending_orders` -> `order-reconciliation-read-model`
- `orderbook_nxt_capability` -> `kr-symbol-universe` for KR missing, otherwise `none`/unsupported
- `quotes`, `ohlcv`, `valuation_fundamentals` -> `provider-contract` until durable read models exist

**Testing:**
- Unit-test representative mappings through `build_invest_coverage`, not just the private helper.
- Assert no actionability field suggests immediate execution or omits approval gates for DB-write/scheduler candidate actions.

---

### Task 4: Fix all-market symbol partitioning

**Objective:** Make `/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT` return KR and US symbol rows.

**File:**
- Modify: `app/services/invest_coverage_service.py`

**Recommended approach:**

1. Keep `_normalize_symbols` unchanged unless needed.
2. Replace the early-return-only logic in `_symbol_rows` with a market expansion layer:

```python
async def _symbol_rows(db, market, symbols, trading_day):
    if not symbols:
        return []
    if market == "all":
        return await _symbol_rows_all_markets(db, symbols, trading_day)
    if market not in {"kr", "us"}:
        return [_unsupported_symbol_row(symbol, market) for symbol in symbols]
    return await _symbol_rows_for_market(db, market, symbols, trading_day)
```

3. Extract the existing KR/US logic into `_symbol_rows_for_market(...)`.
4. Implement `_symbol_rows_all_markets(...)` by partitioning the requested symbols:
   - Symbols matching KR universe (`kr_symbol_universe.symbol`) -> KR.
   - Symbols matching US universe (`us_symbol_universe.symbol`) -> US.
   - If a symbol is not found in either universe, use safe heuristics only as a fallback:
     - six digits -> KR
     - uppercase alphabetic ticker like `AAPL`/`MSFT` -> US
     - `KRW-` or crypto-looking symbols -> unsupported/crypto row if a crypto symbol row contract is desired; otherwise warn that symbol-level diagnostics are not implemented for crypto.
5. Preserve request order in `response.symbols`.
6. Preserve current KR semantics:
   - `investor_flow` supported for KR.
   - `naver_investor_flow` included for KR as candidate/reference diagnostic.
7. Preserve current US semantics:
   - `investor_flow` unsupported.
   - `naver_investor_flow` unsupported.
8. Do not query external providers or scrape Naver/Toss.

**Expected result for the acceptance URL:**
- `005930` row with `market="kr"`.
- `AAPL` row with `market="us"`.
- `MSFT` row with `market="us"`.
- Rows may have `missing`/`stale` states depending on local fixture/test rows, but rows must exist.
- KR row includes `naver_investor_flow` state.
- US rows include `investor_flow="unsupported"` and `naver_investor_flow="unsupported"`.

---

### Task 5: Add backend tests for all-market symbol diagnostics

**Objective:** Lock the ROB-203 acceptance URL into tests.

**File:**
- Modify: `tests/test_invest_coverage.py`

**Add test:**

```python
@pytest.mark.asyncio
async def test_coverage_endpoint_market_all_returns_partitioned_symbol_rows(
    app: FastAPI, db_session
):
    ...
```

**Fixture setup guidance:**
- Use synthetic IDs/symbols if hard-coded real symbols collide with seeded data; however at least one endpoint-level test should use the literal acceptance query `005930,AAPL,MSFT`.
- Insert or ensure universe rows for:
  - `KRSymbolUniverse(symbol="005930", ...)`
  - `USSymbolUniverse(symbol="AAPL", ...)`
  - `USSymbolUniverse(symbol="MSFT", ...)`
- Insert fresh/stale `InvestScreenerSnapshot` rows for at least one KR and one US symbol.
- Insert `InvestorFlowSnapshot` for `005930` with `source="naver_finance"` to verify Naver candidate/reference symbol state stays KR-only.
- Insert `NewsArticle` + `NewsArticleRelatedSymbol` rows for one KR and one US symbol if the current symbol-row contract should mark `news_feed` fresh.

**Assertions:**

```python
r = await client.get("/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT&asOf=2026-05-11")
assert r.status_code == 200
payload = r.json()
assert payload["market"] == "all"
by_symbol = {row["symbol"]: row for row in payload["symbols"]}
assert set(by_symbol) == {"005930", "AAPL", "MSFT"}
assert by_symbol["005930"]["market"] == "kr"
assert by_symbol["AAPL"]["market"] == "us"
assert by_symbol["MSFT"]["market"] == "us"
assert "naver_investor_flow" in by_symbol["005930"]["surfaces"]
assert by_symbol["AAPL"]["surfaces"]["investor_flow"] == "unsupported"
assert by_symbol["MSFT"]["surfaces"]["naver_investor_flow"] == "unsupported"
assert all("actionability" in row for row in payload["symbols"])
```

**Also add service-level test if useful:**
- Directly call `build_invest_coverage(db_session, market="all", symbols=[...], as_of=...)` and assert row ordering matches requested symbol order.

---

## Frontend Plan

### Task 6: Extend TypeScript coverage types

**Objective:** Match backend actionability contract in the frontend.

**File:**
- Modify: `frontend/invest/src/types/coverage.ts`

**Add types matching backend literals:**

```ts
export type CoverageActionPriority = "none" | "low" | "medium" | "high" | "blocked";
export type CoverageActionKind =
  | "none"
  | "monitor"
  | "investigate"
  | "repair_read_model"
  | "backfill_candidate"
  | "scheduler_candidate"
  | "provider_contract_needed"
  | "unsupported_no_action";
export type CoverageApprovalGate =
  | "none"
  | "code_review"
  | "production_db_write_approval"
  | "scheduler_activation_approval"
  | "broker_order_approval";

export interface CoverageActionability {
  priority: CoverageActionPriority;
  action: CoverageActionKind;
  queue?: string | null;
  approvalGates: CoverageApprovalGate[];
  reason?: string | null;
  safeByDefault: boolean;
}
```

Attach to:
- `InvestCoverageSurface.actionability`
- `InvestCoverageSymbol.actionability`

---

### Task 7: Render actionability and Naver readiness in coverage UI

**Objective:** Make `/invest/coverage` an actionable queue view while remaining read-only.

**File:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`

**UI changes:**
1. Keep existing state pills.
2. Keep existing `sourceCandidates` readiness chips for Naver/Toss references.
3. Add an `Actionability` column to the surface table showing:
   - priority pill
   - action kind
   - queue name
   - approval gates as small chips
4. In symbol coverage rows, include:
   - symbol market (`kr`/`us`/`crypto`/unknown)
   - actionability priority/action if not `none`
   - warnings if any
5. Keep right-rail safety principles visible and update them from ROB-201 to ROB-203 wording:
   - source-of-truth is local DB/read-models
   - Toss/Naver are reference/candidate only
   - no orders/backfills/schedulers from this page

**Copy guidance:**
- Avoid wording like “run backfill now” or “execute”.
- Prefer “후보”, “검토”, “승인 필요”, “대기열”.
- Naver chips should be labeled as candidate/reference readiness, not authoritative health.

---

### Task 8: Add targeted frontend tests

**Objective:** Ensure the UI actually renders states, Naver readiness, and actionability metadata.

**Files:**
- Modify/create: `frontend/invest/src/__tests__/coverage.test.tsx` if coverage-specific tests exist or can be added.
- Otherwise extend `frontend/invest/src/__tests__/routes.test.tsx` minimally for route registration and render smoke.

**Test cases:**
1. Mock `fetchInvestCoverage` response with:
   - one `fresh` surface
   - one `provider_unwired` surface with `actionability.priority="blocked"`
   - one Naver source candidate with readiness `request_time_only`
   - symbol rows for `005930`, `AAPL`, `MSFT`
2. Assert rendered text includes:
   - state labels (`정상`, `미연결`, etc.)
   - `naver_finance`
   - readiness label (`request-time` or localized equivalent)
   - action queue (`provider-contract` or localized label)
   - approval gate (`code_review` or localized label)
   - all three symbols.

**Verification commands:**
- Inspect `frontend/invest/package.json` for exact test script.
- Likely commands:

```bash
cd frontend/invest
npm test -- --run coverage
npm run typecheck
```

If the repo uses a different script, record the exact command in handoff.

---

## Documentation Plan

### Task 9: Update coverage docs with work-queue mapping and approval gates

**Objective:** Make the intended use of coverage output explicit and safe.

**File:**
- Modify: `docs/invest-coverage-dashboard.md`

**Add sections:**
1. “Actionability metadata” explaining `priority`, `action`, `queue`, `approvalGates`, `safeByDefault`.
2. “Coverage state to work queue mapping” table:

| State | Meaning | Default action | Approval gate |
| --- | --- | --- | --- |
| fresh | current read-model rows exist | monitor/no action | none |
| stale | old rows exist | investigate or backfill candidate | production DB write approval before any backfill |
| partial | some expected rows exist | repair read model/backfill candidate | production DB write approval before any backfill |
| missing | no local rows exist | investigate/backfill candidate/provider contract | approval depends on queue |
| unsupported | intentionally out of scope | no action | none |
| provider_unwired | concept exists but durable read model absent | provider contract/code work | code review; later DB/scheduler approval if added |
| error | ingestion metadata failed/degraded | investigate | depends on remediation |

3. “All-market symbol diagnostics” documenting:
   - `market=all&symbols=005930,AAPL,MSFT` partitions symbols by KR/US universe/heuristic.
   - KR rows can include `naver_investor_flow` as candidate/reference state.
   - US rows mark investor-flow/Naver investor-flow unsupported.

4. “Approval gates” restating no production writes/backfills/schedulers/broker actions without separate explicit approval.

5. “Naver/Toss semantics”:
   - Toss: benchmark/reference only.
   - Naver: candidate/reference only.
   - Neither becomes `sourceOfTruth`.

---

## Validation Plan

Run all commands from:
`/Users/mgh3326/worktrees/auto_trader/rob-203-coverage-actionability`

### Backend validation

```bash
pytest tests/test_invest_coverage.py -q
```

If imports or DB fixtures require broader context, run the narrow node first:

```bash
pytest tests/test_invest_coverage.py::test_coverage_endpoint_market_all_returns_partitioned_symbol_rows -q
```

Expected: all coverage tests pass and the new all-market symbol test proves rows for `005930`, `AAPL`, `MSFT`.

### Frontend validation

First inspect scripts:

```bash
cd frontend/invest
npm run
```

Then run likely commands:

```bash
npm run typecheck
npm test -- --run
```

If there is a coverage-specific test file:

```bash
npm test -- --run coverage
```

Expected: TypeScript passes and the coverage UI test confirms state chips, Naver readiness chips, actionability metadata, and symbol rows render.

### Read-only/manual smoke

If a local app/test client is available, perform only read-only GET smoke:

```bash
curl 'http://127.0.0.1:<port>/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT'
```

Expected JSON:
- `market: "all"`
- `symbols` contains rows for `005930`, `AAPL`, `MSFT`
- each surface/symbol has `actionability`
- no Naver source candidate has become `sourceOfTruth`

Do not run any production write, backfill, scheduler, or broker command as part of this issue.

---

## Risks and Mitigations

1. **ROB-201 branch dependency:** The current ROB-203 worktree lacks coverage baseline files. Mitigate by integrating ROB-201 first or rebasing after ROB-201 merge before coding ROB-203 deltas.

2. **Symbol market ambiguity under `market=all`:** Some symbols may not exist in local universe tables. Mitigate with universe-first partitioning and conservative fallback heuristics; preserve request order and warning text for unresolved symbols.

3. **Crypto symbol-level scope creep:** ROB-201 `_symbol_rows` only supports KR/US. Keep crypto unsupported unless there is already a durable crypto symbol diagnostic contract.

4. **Actionability becoming execution:** Avoid buttons or language that implies the dashboard can run backfills/schedulers/orders. Metadata is advisory/work-queue only.

5. **Naver semantics regression:** Tests must assert `sourceOfTruth != "naver_finance"` and Naver remains in `sourceCandidates`/references only.

6. **Frontend test brittleness:** Keep tests focused on visible contract text and chips, not exact layout/CSS.

---

## Recommended Implementation Order

1. Integrate ROB-201 baseline into this worktree.
2. Run ROB-201 coverage backend tests to establish baseline.
3. Add backend schema actionability metadata and tests.
4. Add actionability helper/mapping in the service.
5. Fix `market=all` symbol partitioning and preserve order.
6. Add endpoint/service tests for `market=all&symbols=005930,AAPL,MSFT`.
7. Extend frontend TypeScript types.
8. Render actionability metadata in `DesktopCoveragePage.tsx`.
9. Add frontend coverage render tests.
10. Update docs with work-queue mapping and approval gates.
11. Run targeted backend/frontend validation.
12. Post evidence to Linear/Kanban and hand off to review.

---

## Handoff Notes for Implementer

- This is a read-only coverage/actionability issue, not a data-repair issue.
- If you find empty production data while testing, do not backfill it in this task. Report it as a candidate action with the appropriate approval gate.
- If you need to run a local server for smoke, keep it local and read-only.
- If any command would source production env, ensure it does not print secrets and do not run write/backfill/scheduler paths.
- Record actual tool/model used because Claude Code Opus was not available to the planner runtime.
