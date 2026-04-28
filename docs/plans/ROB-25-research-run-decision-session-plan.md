# ROB-25 — Research Run → Live-Refresh → Trading Decision Session

- Linear: https://linear.app/mgh3326/issue/ROB-25/integration-generate-trading-decision-session-from-research-run-live
- Parent: ROB-21
- Worktree: `~/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session`
- Branch: `feature/ROB-25-research-run-decision-session`
- Depends on (already merged to `main`): ROB-22 (`pending_reconciliation_service`), ROB-23 (`nxt_classifier_service`), ROB-24 (`research_run` models / schemas / service / migration — merge `4e5c0486`).
- Does **not** depend on ROB-20. The orchestrator treats every live datum (quote, orderbook, support/resistance, cash, holdings, pending, NXT eligibility) as optional and degrades to warnings in the standard way already exercised by ROB-22/23 tests. **If at implementation time any field actually requires ROB-20 plumbing, stop and report blocker; do not touch ROB-20 from this branch.**

---

## 1. Goal

Add an integration path that takes the latest or selected `ResearchRun` snapshot, performs a **minimal, read-only live refresh** (quote / orderbook / cash / holdings / pending), recomputes pending-order reconciliation and the NXT classifier with the refreshed market context, and persists a `TradingDecisionSession` whose proposals carry decision-support metadata.

**Acceptance criteria mapped:**

| AC | Where it lands |
|----|----------------|
| Accepts a Research Run UUID **or** clear selection criteria | `ResearchRunDecisionSessionRequest` (UUID xor `(market_scope, stage[, strategy_name])`) + `_resolve_research_run` helper |
| Refreshes only the live data needed for decision support | `research_run_live_refresh_service.build_live_refresh_snapshot` — bounded fetcher list, per-symbol failure isolation, no broker mutation imports |
| Proposal payload includes `research_run_id`, `refreshed_at`, `reconciliation_status`, `nxt_eligible` / `venue_eligibility` | Orchestrator builds `original_payload` per §4.4 |
| Returns / verifies the Decision Session URL | New router reuses `build_trading_decision_session_url` and `resolve_trading_decision_base_url` |
| Existing Trading Decision Session flows continue to pass | New router is a separate module so `tests/test_trading_decisions_router_safety.py` keeps its existing forbidden-prefix list; old happy paths re-run unchanged |

---

## 2. Non-negotiable trading safety guardrails

This workstream is **read-only / decision-support only**. Persisting a Decision Session is a decision-ledger event, never an execution authorization.

**Forbidden imports — anywhere in `research_run_decision_session_service` (orchestrator) AND `research_run_live_refresh_service` (provider) AND `research_run_decision_sessions` (router):**

- `app.mcp_server.tooling.orders_registration`
- `app.mcp_server.tooling.orders_modify_cancel`
- `app.mcp_server.tooling.paper_order_handler`
- `app.services.kis_trading_service`
- `app.services.kis_trading_contracts`
- `app.services.fill_notification`
- `app.services.execution_event`
- `app.services.kis_websocket`
- `app.services.kis_websocket_internal`
- `app.services.upbit_websocket`
- any `watch_alert*` / `paper_order*` creator
- `app.tasks` (Prefect / scheduler entrypoints that may execute orders)

**Additionally forbidden in the orchestrator only** (provider may use these for read-only refresh — quotes, orderbook reads, balance/position inquiries):

- `app.services.brokers.*`
- `app.services.kis*` (any KIS read-only client)
- `app.services.upbit*` (Upbit read-only client)
- `app.services.market_data.*`
- `app.services.kis_holdings_service`
- `app.services.manual_holdings_service`

The orchestrator stays fully pure: it consumes a `LiveRefreshSnapshot` DTO and a `ResearchRun` row, produces `ProposalCreate` lists and a `market_brief` blob, and calls only `trading_decision_service.{create_decision_session, add_decision_proposals}` and `research_run_service.get_research_run_by_uuid` / new latest-selector. This is enforced by §6.3 import-safety tests.

**Other guardrails:**
- If TradingAgents is referenced (advisory pass-through; explicitly out of v1 scope but feature-flagged in the request), it must remain `advisory_only=True`, `execution_allowed=False`, and reuse the existing operator path. Default for ROB-25 v1: `include_tradingagents=False`.
- Never log/persist secrets, API keys, account numbers, broker order IDs (already covered by `_redact_stderr` for advisory; orchestrator persists only normalized order IDs already present in `ResearchRun` rows).
- ROB-29 fail-closed for missing KR universe rows is already enforced by ROB-23; the orchestrator must surface the resulting `data_mismatch_requires_review` classifications and `missing_kr_universe` warnings into both the proposal payload and the session `market_brief`.

---

## 3. High-level architecture

```
HTTP POST /trading/api/decisions/from-research-run
        │
        ▼
app/routers/research_run_decision_sessions.py            (NEW, dedicated router)
        │  resolves research_run, calls provider, calls orchestrator
        ▼
app/services/research_run_live_refresh_service.py        (NEW, IMPURE: read-only KIS/Upbit calls)
        │  returns LiveRefreshSnapshot DTO
        ▼
app/services/research_run_decision_session_service.py    (NEW, PURE)
        │  fan-out per ResearchRunCandidate:
        │    – pair to ResearchRunPendingReconciliation by order_id (if any)
        │    – call reconcile_pending_order() with refreshed context
        │    – call classify_nxt_pending_order/_candidate/_holding (KR only)
        │    – build ProposalCreate with research_run_id + refreshed_at + recon/nxt summary
        │  build market_brief with summaries
        │  persist TradingDecisionSession + proposals via trading_decision_service
        ▼
TradingDecisionSession (UUID) → URL via build_trading_decision_session_url
```

**Why a new router (not an extension of `app/routers/trading_decisions.py`):** the existing `tests/test_trading_decisions_router_safety.py` forbids `app.services.brokers`, `app.services.kis*`, `app.services.upbit*`, etc. Importing the live-refresh provider transitively into `trading_decisions.py` would fail that test. A dedicated router with its own — narrower — safety test (forbidding **mutation** paths, allowing read-only KIS/Upbit) keeps both flows safe and independently auditable.

---

## 4. Module-by-module design

### 4.1 Schemas — `app/schemas/research_run_decision_session.py` (NEW)

```python
class ResearchRunSelector(BaseModel):
    # Exactly one of run_uuid OR (market_scope + stage [+ strategy_name + status])
    run_uuid: UUID | None = None
    market_scope: MarketScopeLiteral | None = None
    stage: StageLiteral | None = None
    strategy_name: str | None = None
    status: RunStatusLiteral | None = "open"

    @model_validator(mode="after")
    def _xor(self) -> Self: ...   # raises if both or neither provided

class ResearchRunDecisionSessionRequest(BaseModel):
    selector: ResearchRunSelector
    include_tradingagents: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    generated_at: datetime | None = None  # default = now(UTC) at service layer

class LiveRefreshQuote(BaseModel):
    price: Decimal
    as_of: datetime

class LiveRefreshSnapshot(BaseModel):
    refreshed_at: datetime
    quote_by_symbol: dict[str, LiveRefreshQuote]
    orderbook_by_symbol: dict[str, OrderbookContext]      # reuse ROB-22 DTO
    support_resistance_by_symbol: dict[str, SupportResistanceContext]  # ROB-22 DTO; may be {}
    kr_universe_by_symbol: dict[str, KrUniverseContext]   # ROB-22 DTO; required for KR symbols
    cash_balances: dict[str, Decimal]                     # currency→amount, e.g. {"KRW": ...}
    holdings_by_symbol: dict[str, Decimal]                # symbol→qty (signed=long-only here)
    pending_orders: list[PendingOrderInput]               # ROB-22 DTO; live, refreshed
    warnings: list[str]                                   # token list, e.g. ["quote_failed:000660"]

class ResearchRunDecisionSessionResponse(BaseModel):
    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    research_run_uuid: UUID
    refreshed_at: datetime
    proposal_count: int
    reconciliation_count: int
    advisory_used: bool = False
    advisory_skipped_reason: str | None = None
    warnings: list[str] = []
```

### 4.2 Orchestrator — `app/services/research_run_decision_session_service.py` (NEW, PURE)

Public API:

```python
@dataclass(frozen=True)
class ResearchRunDecisionSessionResult:
    session: TradingDecisionSession
    research_run: ResearchRun
    refreshed_at: datetime
    proposal_count: int
    reconciliation_count: int
    warnings: tuple[str, ...]

async def resolve_research_run(
    db: AsyncSession, *, user_id: int, selector: ResearchRunSelector
) -> ResearchRun: ...
    # raises ResearchRunNotFound when not visible to user_id

async def create_decision_session_from_research_run(
    db: AsyncSession,
    *,
    user_id: int,
    research_run: ResearchRun,
    snapshot: LiveRefreshSnapshot,
    request: ResearchRunDecisionSessionRequest,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ResearchRunDecisionSessionResult: ...
```

Internal pipeline (per candidate from `research_run.candidates`, deterministic order):

1. **Pair pending recon row.** If `candidate.candidate_kind == "pending_order"` and a `ResearchRunPendingReconciliation` exists with matching `(symbol, side)` (or matching `candidate_id`), keep its persisted `classification` / `nxt_classification` as the **research-time baseline**.
2. **Refresh classification.** Build `MarketContextInput` from `snapshot` for that symbol (KR universe, quote, orderbook, support/resistance). Call:
   - `pending_order` → `pending_reconciliation_service.reconcile_pending_order(...)` for the live order, then `nxt_classifier_service.classify_nxt_pending_order(...)` (KR only) for NXT label.
   - `holding` → `nxt_classifier_service.classify_nxt_holding(...)` (KR only).
   - `screener_hit` / `proposed` / `other` → `nxt_classifier_service.classify_nxt_candidate(...)` if KR with a `proposed_price`, else mark `kind=candidate, classification="unknown"` with reason `"no_proposed_price"`.
3. **Determine venue eligibility.**
   - KR: `nxt_eligible = snapshot.kr_universe_by_symbol[symbol].nxt_eligible` (None if missing — surfaces `missing_kr_universe`).
   - US / crypto: `venue_eligibility = {"nxt": False}`, `nxt_eligible = None`.
4. **Pick `proposal_kind`.**
   - `pending_order` → `other` (decision-ledger entry, not new direction).
   - `holding` → `no_action` (watch-only).
   - `screener_hit` / `proposed` → carry `candidate.payload.get("proposal_kind")` if it parses to `ProposalKind`, else `other`.
5. **Build `original_payload`** (§4.4).
6. **Aggregate warnings** for `market_brief`.
7. After loop: `trading_decision_service.create_decision_session` + `add_decision_proposals`. `source_profile = "research_run"`. `strategy_name` carried from `research_run.strategy_name`. `market_scope = research_run.market_scope`.

**Idempotency & error policy:**
- If `research_run.candidates` is empty → raise `EmptyResearchRunError`; the router maps to 422.
- If the resolved run does not belong to `user_id` → raise `ResearchRunNotFound`; router maps to 404 (no leak between users).
- DB transaction: do **not** commit inside the service. Router handles commit, mirroring the existing operator pattern.

### 4.3 Live-refresh provider — `app/services/research_run_live_refresh_service.py` (NEW, IMPURE)

Public API:

```python
async def build_live_refresh_snapshot(
    db: AsyncSession,
    *,
    research_run: ResearchRun,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    timeout_seconds: float = 8.0,
) -> LiveRefreshSnapshot: ...
```

Implementation rules:

- Collect the unique symbol set from `research_run.candidates` and `research_run.reconciliations` (preserving market_scope so KR/US/crypto calls go to the right adapter).
- For each symbol, fetch the **minimum**:
  - **Quote** — `app.services.market_data.get_quote` (already exists, market-aware).
  - **Orderbook** — `app.services.market_data.get_orderbook` (KR + KRW-crypto only). For US, omit and add warning token `orderbook_unavailable_us`.
  - **Support / resistance** — _optional_. For v1 use whatever the existing market_data layer already exposes; if nothing, leave the dict empty. Do **not** add new technical computations in this slice.
  - **KR universe** — `kr_symbol_universe_service.is_nxt_eligible(symbol, db=db)` per KR symbol; mark missing rows with warning `missing_kr_universe:{symbol}`.
- One-shot fetches (per market scope of the run):
  - **Cash** — KR/US: `kis_holdings_service` read APIs; crypto: Upbit `fetch_balances`.
  - **Holdings** — same source as cash; symbol-keyed dict.
  - **Pending orders** — `app.mcp_server.tooling.orders_history.get_order_history_impl(status="pending", market=<scope>, is_mock=False)`. This is the **read-only** wrapper that already exists and is allowed in the safety test (see §2 — `orders_history` is allowed; `orders_modify_cancel` and `orders_registration` are forbidden).
- Use `asyncio.wait_for` per call with a tight per-call timeout (~`timeout_seconds / N`), and gather with `return_exceptions=True`. A failure of one call appends a warning token (e.g., `quote_failed:AAPL`) and continues; it never raises.
- Return value: a fully populated `LiveRefreshSnapshot`. `refreshed_at = now()` AFTER the gather, so it reflects the freshest single point in time consumers can reason about.

**Critical:** the provider may import read-only KIS/Upbit clients and `app.services.market_data`, but must **not** import any module from §2 forbidden list.

### 4.4 Proposal `original_payload` contract

```jsonc
{
  "advisory_only": true,
  "execution_allowed": false,
  "research_run_id": "<run_uuid>",
  "research_run_candidate_id": "<candidate_uuid>",
  "refreshed_at": "<iso8601 utc>",
  "reconciliation_status": "maintain | near_fill | too_far | chasing_risk | data_mismatch | kr_pending_non_nxt | unknown_venue | unknown | null",
  "nxt_classification": "<NxtClassification>|null",
  "nxt_eligible": true | false | null,
  "venue_eligibility": { "nxt": true | false | null },
  "live_quote": { "price": "<decimal>", "as_of": "<iso8601>" } | null,
  "pending_order_id": "<order_id>|null",
  "decision_support": { ...recon decision_support DTO as JSON-safe dict... },
  "source_freshness": { ...candidate.source_freshness pass-through... },
  "warnings": ["quote_failed:000660", "missing_kr_universe", "..."],
  "candidate_kind": "<pending_order|holding|screener_hit|proposed|other>"
}
```

Session `market_brief`:

```jsonc
{
  "advisory_only": true,
  "execution_allowed": false,
  "research_run_uuid": "<run_uuid>",
  "refreshed_at": "<iso8601>",
  "counts": { "candidates": N, "reconciliations": M },
  "reconciliation_summary": { "maintain": x, "near_fill": x, "too_far": x, "chasing_risk": x, "data_mismatch": x, "kr_pending_non_nxt": x, "unknown_venue": x, "unknown": x },
  "nxt_summary": { "actionable": x, "too_far": x, "non_nxt": x, "watch_only": x, "data_mismatch_requires_review": x, "unknown": x },
  "snapshot_warnings": [...],
  "source_warnings": [...]
}
```

### 4.5 Router — `app/routers/research_run_decision_sessions.py` (NEW)

- Mounted alongside `trading_decisions.router` in `app/main.py`.
- Single endpoint: `POST /trading/api/decisions/from-research-run`, response `ResearchRunDecisionSessionResponse`, status `201`.
- Auth: `Depends(get_authenticated_user)`, db: `Depends(get_db)`.
- Flow: resolve run → build snapshot → create session → `db.commit()` → build URL → return.
- Error mapping:
  - `ResearchRunNotFound` → `404 research_run_not_found`
  - `EmptyResearchRunError` → `422 research_run_has_no_candidates`
  - `LiveRefreshTimeout` (provider-level) → `504 live_refresh_timeout`
  - Validation errors handled by FastAPI default `422`.
- `Location` header set to `/trading/api/decisions/{session_uuid}` for parity with the operator endpoint.

### 4.6 Wiring in `app/main.py`

Register the new router after `trading_decisions.router` (alphabetical/feature grouping). No other edits to `main.py`. No new settings required (reuse `settings.public_base_url`).

---

## 5. Selection logic for "latest" research run

`resolve_research_run` (in §4.2):

1. If `selector.run_uuid` is set → `research_run_service.get_research_run_by_uuid(db, run_uuid=..., user_id=...)`. None → `ResearchRunNotFound`.
2. Else require `selector.market_scope` and `selector.stage`. New helper in `research_run_service` (additive, not a refactor of existing code):
   ```python
   async def get_latest_research_run(
       db, *, user_id, market_scope, stage,
       strategy_name: str | None = None,
       status: str | None = "open",
   ) -> ResearchRun | None:
       # ORDER BY generated_at DESC LIMIT 1
       # eager-load candidates and reconciliations (selectinload), same as get_research_run_by_uuid
   ```
3. None → `ResearchRunNotFound`.

Tie-breaker on identical `generated_at`: ORDER BY `id DESC` for determinism.

---

## 6. Tests

All new tests use `pytest.mark.unit` unless noted. Existing tests in `tests/test_trading_decisions_router*.py`, `tests/test_operator_decision_session_schemas.py`, and `tests/test_research_run_*` must continue to pass — included in the regression sweep below.

### 6.1 Schema tests — `tests/test_research_run_decision_session_schemas.py` (NEW)

- `ResearchRunSelector` xor invariant (UUID xor (scope+stage)).
- `LiveRefreshSnapshot` round-trips Decimal-as-string.
- `ResearchRunDecisionSessionResponse` shape.

### 6.2 Orchestrator service tests — `tests/test_research_run_decision_session_service.py` (NEW)

Use stub `ResearchRun` rows (in-memory ORM via `Session(expire_on_commit=False)`) and a hand-built `LiveRefreshSnapshot`.

- Happy path KR: 3 candidates (1 pending_order, 1 holding, 1 proposed) → 3 proposals; `original_payload` carries all required keys (§4.4); session `market_brief` has correct `counts` and `reconciliation_summary`.
- Pairing: a candidate with `kind=pending_order` and matching `order_id` reuses the persisted research-time recon row but re-classifies with refreshed quote — proposal payload contains both `reconciliation_status` (refreshed) and the original recon's `summary` under `decision_support`.
- ROB-29 fail-closed: KR pending order whose symbol is missing from `kr_universe_by_symbol` → `nxt_classification == "data_mismatch_requires_review"`, `nxt_eligible is None`, payload `warnings` includes `"missing_kr_universe"`, `market_brief.snapshot_warnings` contains `f"missing_kr_universe:{symbol}"`.
- US run: candidates with `instrument_type=equity_us` skip NXT classification; `venue_eligibility == {"nxt": false}`.
- Crypto run: same — no KR-universe lookup attempted.
- `EmptyResearchRunError` raised when `candidates` is empty.
- `ResearchRunNotFound` raised when run.user_id != user_id (via `resolve_research_run`).
- Latest-selector: returns most recent open run, ignores closed/archived, ignores other users.
- Determinism: proposal order matches candidate order (by `candidate.id ASC`).
- `include_tradingagents=True` is **rejected with `NotImplementedError`** in v1 (router maps to 501) — keeps the advisory pass-through out of scope while preserving the field for ROB-26.

### 6.3 Import-safety tests (CRITICAL)

#### `tests/test_research_run_decision_session_service_safety.py` (NEW)

Same fresh-process pattern as `test_trading_decisions_router_safety.py`. After importing only `app.services.research_run_decision_session_service`, assert NONE of these prefixes appear in `sys.modules`:

```python
FORBIDDEN_PREFIXES = [
    "app.services.brokers",
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_holdings_service",
    "app.services.manual_holdings_service",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.market_data",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.paper_order_handler",
    "app.tasks",
]
```

#### `tests/test_research_run_decision_session_router_safety.py` (NEW)

Same pattern, but tighter — only **mutation** prefixes are forbidden (read-only KIS/Upbit/market_data is allowed because the live-refresh provider needs them):

```python
FORBIDDEN_MUTATION_PREFIXES = [
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.paper_order_handler",
    "app.tasks",
]
```

### 6.4 Provider tests — `tests/test_research_run_live_refresh_service.py` (NEW)

Patch `app.services.market_data.{get_quote,get_orderbook}`, `kr_symbol_universe_service.is_nxt_eligible`, `orders_history.get_order_history_impl`, and KIS/Upbit balance/holdings reads with `unittest.mock.AsyncMock`. Assert:

- Builds `LiveRefreshSnapshot` with one `quote_by_symbol` entry per unique candidate symbol.
- One symbol's `get_quote` raises → snapshot still returned, with `quote_failed:{symbol}` warning.
- US scope skips orderbook fetch and emits `orderbook_unavailable_us`.
- KR symbol absent from universe → `kr_universe_by_symbol[symbol]` omitted, `missing_kr_universe:{symbol}` warning.
- `refreshed_at` is set after gather completes (assert it's after the patched start time).
- Provider does not invoke any forbidden mutation tool (covered by §6.3 safety test plus an explicit assertion that `orders_history.get_order_history_impl` was called only with `status="pending"` arg).

### 6.5 Router tests — `tests/test_research_run_decision_session_router.py` (NEW)

FastAPI TestClient with a real-but-isolated DB session (existing fixture from `tests/conftest.py`):

- 201 happy path with `selector.run_uuid`: response payload matches schema, `session_url` resolvable, DB has `TradingDecisionSession` with proposal_count == candidates count, no `TradingDecisionAction` rows created.
- 201 latest-selection path: `selector = {market_scope, stage}` picks the most recent matching run.
- 404 unknown UUID.
- 404 UUID belonging to a different user (no information leak).
- 422 empty candidates.
- 501 `include_tradingagents=True` (v1 explicitly out of scope).
- Existing `tests/test_trading_decisions_router_safety.py` still passes (no changes to `app/routers/trading_decisions.py`).

### 6.6 Regression sweep (run after every step)

```bash
uv run pytest \
  tests/test_research_run_schemas.py \
  tests/test_research_run_decision_session_schemas.py \
  tests/test_research_run_decision_session_service.py \
  tests/test_research_run_decision_session_service_safety.py \
  tests/test_research_run_live_refresh_service.py \
  tests/test_research_run_decision_session_router.py \
  tests/test_research_run_decision_session_router_safety.py \
  tests/test_trading_decisions_router.py \
  tests/test_trading_decisions_router_safety.py \
  tests/test_trading_decisions_spa_router.py \
  tests/test_trading_decisions_spa_router_safety.py \
  tests/test_trading_decision_session_url.py \
  tests/test_operator_decision_session_schemas.py \
  -v
```

Plus `make typecheck`, `make lint`.

---

## 7. Implementation order (TDD; one bite at a time)

1. **Schemas + tests** — `app/schemas/research_run_decision_session.py`, `tests/test_research_run_decision_session_schemas.py`. Red → green.
2. **`get_latest_research_run` helper + tests** — additive in `app/services/research_run_service.py`; extend `tests/test_research_run_*` (or add a new test module) to cover ordering & user isolation. Red → green.
3. **Orchestrator service + tests** — `app/services/research_run_decision_session_service.py`, `tests/test_research_run_decision_session_service.py`, `tests/test_research_run_decision_session_service_safety.py`. Red → green. **Do not touch broker/market_data here.**
4. **Live-refresh provider + tests** — `app/services/research_run_live_refresh_service.py`, `tests/test_research_run_live_refresh_service.py`. Red → green.
5. **Router + tests** — `app/routers/research_run_decision_sessions.py`, register in `app/main.py`, `tests/test_research_run_decision_session_router.py`, `tests/test_research_run_decision_session_router_safety.py`. Red → green.
6. **Regression sweep** — §6.6 plus `make typecheck` and `make lint`.
7. **Smoke** — boot dev server, hit endpoint with a known seeded research run via curl, eyeball response. Do **not** run against production data.

No DB migration. No changes to ROB-22/23/24 modules. No changes to `app/routers/trading_decisions.py` beyond what's necessary for `app/main.py` registration (none expected).

---

## 8. Risks & rollback

| Risk | Mitigation |
|------|-----------|
| Live-refresh latency spikes during KR pre-open | Per-call `asyncio.wait_for`, `gather(return_exceptions=True)`, end-to-end `timeout_seconds=8.0` configurable; partial failures degrade to warnings, never raise |
| KR universe DB stale on a brand-new symbol | ROB-23 fail-closed already covers this (`data_mismatch_requires_review`); the orchestrator surfaces it in payload and `market_brief.snapshot_warnings` |
| Future maintainers add a broker mutation import to the new orchestrator | Import-safety test §6.3 fails CI fast |
| `orders_history.get_order_history_impl` transitively imports `orders_modify_cancel` (already does) | Allowed because that helper is read-only normalizer code; the router safety test forbids only mutation **entry-points** (`orders_registration`, `orders_modify_cancel`'s public `cancel_order_impl`/`modify_order_impl`), not the file itself. Verify by asserting `cancel_order_impl`/`modify_order_impl` are not called (mock-and-assert in router test) — this is belt-and-braces alongside the import test. |
| ROB-26 later adds a TradingAgents pass-through and inadvertently relaxes safety | `include_tradingagents=True` is explicitly 501 in v1; ROB-26 must add its own safety tests before flipping the flag |

**Rollback:** the change set is exactly:

- 4 new files: `app/schemas/research_run_decision_session.py`, `app/services/research_run_decision_session_service.py`, `app/services/research_run_live_refresh_service.py`, `app/routers/research_run_decision_sessions.py`.
- 1 small edit: `app/main.py` (router registration).
- 1 additive helper: `get_latest_research_run` in `app/services/research_run_service.py`.
- 6 new test files (§6).

To roll back: revert the merge commit. No data migration to undo, no schema change.

---

## 9. Blockers

**None at planning time.** All persisted models / DTOs / classifier services from ROB-22/23/24 are present on `main` and verified above. ROB-20 is not required because every live datum the orchestrator consumes is optional and degrades to a warning. If at implementation time the team decides support/resistance MUST be populated and this requires ROB-20 plumbing, **stop and report a blocker**; do not modify ROB-20 from this branch.

---

## 10. Handoff to OpenCode / Kimi implementer (same session, same worktree)

**Worktree:** `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-25-research-run-decision-session`
**Branch:** `feature/ROB-25-research-run-decision-session` (do not switch).

Briefing for the implementer agent:

> Implement ROB-25 strictly per `docs/plans/ROB-25-research-run-decision-session-plan.md`. Follow the §7 step order TDD-style: write the test, see it fail, write the minimum code to pass, repeat. Do **not** touch broker / order mutation modules anywhere in the new files (see §2 forbidden lists). Do **not** edit ROB-20/22/23/24 sources. Do **not** create DB migrations. After each step, run the relevant subset of §6.6; after step 5, run the full §6.6 sweep plus `make typecheck` and `make lint`. If §6.3 import-safety tests fail, that is a stop-the-line — refactor the new code to remove the offending import; do not relax the test list. If you discover the live-refresh provider needs a data source not enumerated in §4.3, stop and surface it in the PR description as a follow-up rather than expanding scope. Commit message scope: `feat(research): create decision session from research run live refresh (ROB-25)`. Co-author trailer per CLAUDE.md.

Reviewer (planner) returns at: post-implementation review against this plan + the safety-test outputs + the regression sweep.
