# ROB-211 K0 — Execution Ledger Vertical Slice (Design)

**Status:** Proposed — planner-only artifact.
**Owner:** ROB-211 (Robin / Paperclip).
**Date:** 2026-05-13.
**Branch / Worktree:** `rob-211-execution-ledger` at `/Users/mgh3326/work/auto_trader/.worktrees/rob-211-execution-ledger`.
**Upstream:** PR #800 (commit `52b6daba`) stabilized KIS overseas + Upbit filled-order REST collection — this is the dependency that makes ROB-211 read-side safe.

---

## 1. Purpose

Establish a single, **idempotent, broker-canonical execution ledger** that records executed fills (KIS domestic, KIS overseas, Upbit) plus the reconciliation diagnostics required to keep it in sync with each broker's REST history. Provide a **read-only `/invest` projection** so dashboards (and future automation) can query fills and sell history without touching live broker code paths.

This is a vertical-slice **K0** — schema + service + reconciler + read API. It ships **inert** (commit-disabled by default; scheduler unregistered) and is activated only after dry-run evidence is reviewed.

### Why now
- PR #800 stabilized the read surfaces (`fetch_closed_orders` / KIS overseas filled history). The remaining gap is persistence + idempotency + a query surface.
- Existing per-broker tables (`review.alpaca_paper_order_ledger`, `review.kis_mock_order_ledger`, `pending_orders`) cover *paper*, *mock*, and *open* orders respectively. There is no canonical store for **live filled orders across brokers**.
- `review.trades` exists but is a manual review/booking surface (unique on `(account, order_id)` with KRW/USD-only currency CHECK and no broker dimension) — repurposing it would tangle review workflows with broker reconciliation.

### Non-goals (this slice)
- No broker order submit / cancel / modify (read-only REST consumption).
- No Toss / manual-holdings ingestion (out of scope; KIS + Upbit broker history is canonical).
- No frontend changes — `/invest` UI consumes the new endpoints in a follow-up.
- No Prefect scheduler deployment (TaskIQ task ships scheduleless; production cadence registered separately, paused).
- No backfill migration of historical fills (additive table; first commit run starts the window).

---

## 2. Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|--------------|
| AC1 | New additive table `review.execution_ledger` exists with broker-canonical unique constraint | Alembic upgrade/downgrade roundtrip test |
| AC2 | All writes flow through `ExecutionLedgerRepository.upsert_fill`; no direct SQL writes in repo | Grep test + reviewer checklist |
| AC3 | `ExecutionLedgerReconciler.run(broker, window_hours, dry_run=True)` produces a structured diff (would-insert / would-update / unchanged) without DB writes when `dry_run=True` | Unit test with mocked broker fixtures |
| AC4 | Same reconciler, `dry_run=False` requires `EXECUTION_LEDGER_COMMIT_ENABLED=True` env flag; otherwise raises | Unit test |
| AC5 | Re-running the reconciler against the same broker window is idempotent (no duplicate rows, same diff) | Integration test (real Postgres, mocked broker) |
| AC6 | `GET /trading/api/invest/fills/recent`, `/by-symbol/{symbol}`, `/sell-history`, `/freshness` return paginated JSON | Router tests; auth required via `get_authenticated_user` |
| AC7 | `/freshness` returns `dataState ∈ {"fresh", "stale", "missing"}` per broker mirroring ROB-204/ROB-208 pattern | Router test |
| AC8 | CLI `scripts/reconcile_execution_ledger.py` defaults to `--dry-run`; `--commit` requires the env flag | CLI test |
| AC9 | No broker mutation surfaces (order_submit, cancel, modify) imported anywhere under `app/services/execution_ledger/` | Grep test |
| AC10 | Runbook `docs/runbooks/execution-ledger.md` documents activation checklist (env flag flip, first commit run, scheduler unpause) | Reviewer + runbook diff |

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  Brokers (read-only REST, stabilized by PR #800)            │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ Upbit            │  │ KIS Domestic     │  │ KIS Overseas     │          │
│  │ fetch_closed_*   │  │ inquire_daily_*  │  │ fetch_closed_*   │          │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘          │
└───────────┼─────────────────────┼─────────────────────┼────────────────────┘
            │                     │                     │
            └─────────────────────┼─────────────────────┘
                                  ▼
            ┌──────────────────────────────────────┐
            │ app/services/execution_ledger/        │
            │  ├─ normalizers.py                    │  reuses n8n_filled_orders_service helpers
            │  ├─ reconciler.py  (dry-run gated)    │  builds diff; writes via repository
            │  ├─ repository.py  (only write surface)│  INSERT ... ON CONFLICT DO UPDATE
            │  └─ query_service.py (read-only)      │  /invest projection
            └──────────────────────────────────────┘
                                  │
                                  ▼
                  ┌─────────────────────────────┐
                  │ review.execution_ledger     │
                  └─────────────────────────────┘
                                  │
                                  ▼
            ┌────────────────────────────────────┐
            │ app/routers/invest_fills.py (GET)  │
            │  /trading/api/invest/fills/*       │
            └────────────────────────────────────┘
```

**Boundary rules** (enforced by import-graph + grep tests):
- `app/services/execution_ledger/` MAY import `app/services/brokers/*/orders.py` read functions.
- `app/services/execution_ledger/` MUST NOT import any function that mutates broker state.
- `app/services/execution_ledger/repository.py` is the **only** module that issues `INSERT` / `UPDATE` against `review.execution_ledger`.
- `app/routers/invest_fills.py` accepts only `GET`.

---

## 4. Data Model

### 4.1 New table — `review.execution_ledger`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `broker` | TEXT NOT NULL | CHECK IN (`'kis'`, `'upbit'`) — extend later for `alpaca` if needed |
| `account_mode` | TEXT NOT NULL | CHECK IN (`'live'`, `'mock'`) — paper/Alpaca stay in their own tables |
| `venue` | TEXT NOT NULL | KR / US / CRYPTO (KIS exchange code or `'upbit_krw'`) |
| `instrument_type` | ENUM `instrument_type` | reuse existing PG enum |
| `symbol` | TEXT NOT NULL | DB canonical form (`.` for US, `KRW-BTC` raw for Upbit normalized to base) |
| `raw_symbol` | TEXT NOT NULL | original broker symbol for audit |
| `side` | TEXT NOT NULL | CHECK IN (`'buy'`, `'sell'`) |
| `broker_order_id` | TEXT NOT NULL | exchange-assigned order ID |
| `fill_seq` | INTEGER NOT NULL DEFAULT 0 | partial-fill sequence (KIS: per-fill index; Upbit: 0 — aggregate-only) |
| `filled_qty` | NUMERIC(20,8) NOT NULL | |
| `filled_price` | NUMERIC(20,8) NOT NULL | |
| `filled_notional` | NUMERIC(20,4) NOT NULL | qty × price (server-computed) |
| `fee_amount` | NUMERIC(20,4) | nullable for Upbit fees that arrive separately |
| `fee_currency` | TEXT | `'KRW'` / `'USD'` |
| `filled_at` | TIMESTAMPTZ NOT NULL | broker-reported execution time (KST or broker-native, stored as TZ-aware) |
| `currency` | TEXT NOT NULL | CHECK IN (`'KRW'`, `'USD'`) |
| `correlation_id` | TEXT | optional client-supplied tag (e.g. watch-order-intent UUID) |
| `source` | TEXT NOT NULL | CHECK IN (`'reconciler'`, `'websocket'`, `'manual_import'`) — only `'reconciler'` written in K0 |
| `source_run_id` | UUID | reconciler-run identifier for traceability |
| `raw_payload_json` | JSONB | sanitized broker response (PII / API-key keys redacted) |
| `created_at` | TIMESTAMPTZ default now() | |
| `updated_at` | TIMESTAMPTZ default now() onupdate | |

**Constraints / Indexes**:
- `UNIQUE (broker, broker_order_id, fill_seq)` — partial unique, named `uq_execution_ledger_broker_order_fill`. This is the idempotency key.
- CHECK constraints on `broker`, `account_mode`, `side`, `currency`, `source`.
- `INDEX ix_execution_ledger_filled_at (filled_at DESC)`.
- `INDEX ix_execution_ledger_symbol_filled_at (symbol, filled_at DESC)`.
- `INDEX ix_execution_ledger_broker_filled_at (broker, filled_at DESC)`.
- `INDEX ix_execution_ledger_source_run_id (source_run_id)`.

**Migration**: `alembic/versions/2026_05_13_rob211_add_execution_ledger.py` — additive only. No data backfill in this revision. Downgrade drops table.

**ORM model**: `app/models/execution_ledger.py` → `class ExecutionLedger(Base)` in `review` schema. Mirrors `AlpacaPaperOrderLedger`'s stylistic conventions (Mapped[...] typings, `__table_args__` tuple, PG_UUID for source_run_id).

### 4.2 Companion table — `review.execution_ledger_reconcile_runs`

Optional but recommended for diagnostics (matches `MarketEventIngestionPartition` pattern):

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID PK | |
| `broker` | TEXT NOT NULL | |
| `window_start` | TIMESTAMPTZ NOT NULL | inclusive |
| `window_end` | TIMESTAMPTZ NOT NULL | exclusive |
| `started_at` | TIMESTAMPTZ default now() | |
| `finished_at` | TIMESTAMPTZ | |
| `dry_run` | BOOL NOT NULL | |
| `would_insert` | INTEGER NOT NULL DEFAULT 0 | |
| `would_update` | INTEGER NOT NULL DEFAULT 0 | |
| `unchanged` | INTEGER NOT NULL DEFAULT 0 | |
| `committed_insert` | INTEGER NOT NULL DEFAULT 0 | 0 when dry-run |
| `committed_update` | INTEGER NOT NULL DEFAULT 0 | 0 when dry-run |
| `error_summary` | TEXT | |
| `notes` | TEXT | |

Indexed on `(broker, window_start)` and `(started_at DESC)`. Drives `/fills/freshness` (last-success-per-broker).

---

## 5. Service Boundaries

### 5.1 `ExecutionLedgerRepository` (write surface)

```python
class ExecutionLedgerRepository:
    def __init__(self, db: AsyncSession): ...

    async def upsert_fill(self, fill: ExecutionLedgerUpsert) -> ExecutionLedgerUpsertResult:
        """INSERT ... ON CONFLICT (broker, broker_order_id, fill_seq) DO UPDATE.
        Returns ('inserted' | 'updated' | 'unchanged', row_id).
        """

    async def record_run(self, run: ReconcileRunRecord) -> None: ...
    async def latest_run_per_broker(self) -> dict[str, ReconcileRunRecord]: ...
```

- `ExecutionLedgerUpsert` is a Pydantic v2 schema in `app/schemas/execution_ledger.py`.
- `upsert_fill` returns `'unchanged'` when no non-metadata columns differ. This is what powers idempotent diffs.

### 5.2 `ExecutionLedgerReconciler`

```python
class ExecutionLedgerReconciler:
    def __init__(self, repo: ExecutionLedgerRepository, brokers: BrokerHistoryClients): ...

    async def run(
        self,
        broker: Literal["kis", "upbit"],
        *,
        window_hours: int = 24,
        dry_run: bool = True,
    ) -> ReconcileDiff:
        """Fetch recent filled orders → normalize → compute diff → optionally commit."""
```

- When `dry_run=False`, **must** assert `settings.EXECUTION_LEDGER_COMMIT_ENABLED is True` — otherwise raise `ExecutionLedgerCommitDisabledError`.
- KIS path: calls `KISClient.overseas_orders.fetch_closed_orders` + KIS domestic `inquire_daily_ccld` helper.
- Upbit path: calls `app.services.brokers.upbit.orders.fetch_closed_orders` paginated.
- Normalization delegates to a thin wrapper over `app/services/n8n_filled_orders_service._normalize_*` so we have **one** normalization rule across the codebase. (Refactor to extract pure functions into `app/services/execution_ledger/normalizers.py` if `n8n_filled_orders_service` couples them to its own pipeline.)
- Writes `review.execution_ledger_reconcile_runs` row at end of every run (dry-run or commit).

### 5.3 `ExecutionLedgerQueryService` (read-only)

```python
class ExecutionLedgerQueryService:
    async def list_recent(self, *, limit: int, market: str | None) -> list[ExecutionLedgerRead]: ...
    async def list_by_symbol(self, *, symbol: str, days: int) -> list[ExecutionLedgerRead]: ...
    async def list_sell_history(self, *, days: int, market: str | None) -> list[ExecutionLedgerRead]: ...
    async def freshness(self) -> FreshnessReport: ...
```

`market` filter is `"kr" | "us" | "crypto"` and maps to `(broker, venue)` predicates.

### 5.4 Router — `app/routers/invest_fills.py`

```
GET /trading/api/invest/fills/recent?limit=50&market=kr|us|crypto
GET /trading/api/invest/fills/by-symbol/{symbol}?days=30
GET /trading/api/invest/fills/sell-history?days=30&market=...
GET /trading/api/invest/fills/freshness
```

- All depend on `get_authenticated_user`.
- All `response_model=` types live in `app/schemas/execution_ledger.py`.
- `freshness` returns per-broker `{broker, last_run_at, lag_minutes, dataState, last_run_id, notes}`. `dataState`:
  - `"fresh"` — most recent successful run finished < 2× window_hours ago.
  - `"stale"` — within 24h × 3 but past freshness threshold.
  - `"missing"` — no successful run on record.

### 5.5 CLI

`scripts/reconcile_execution_ledger.py`:

```
uv run python -m scripts.reconcile_execution_ledger \
  --broker kis|upbit \
  --window-hours 24 \
  [--dry-run | --commit]
```

- Default = `--dry-run`. Mutually exclusive with `--commit`.
- `--commit` requires `EXECUTION_LEDGER_COMMIT_ENABLED=True`.
- Prints `ReconcileDiff` summary + writes a single row to `execution_ledger_reconcile_runs`.

### 5.6 TaskIQ task (registered, scheduleless)

`app/tasks/execution_ledger.py::reconcile_execution_ledger_smoke` — analogous to `research_reports.ingest_bulk_smoke`. Registered for manual invocation; production cadence is registered in `robin-prefect-automations` and ships `paused=true`.

---

## 6. Safety & Gates

| Gate | Required For | Evidence |
|---|---|---|
| **G0** (in this PR) | Merge | Code review + alembic upgrade/downgrade test green + grep tests confirming no broker mutation imports |
| **G1** (post-merge) | First dry-run runs in production | None — ledger ships inert; dry-run is read-only and safe |
| **G2** (separate ops change) | Flip `EXECUTION_LEDGER_COMMIT_ENABLED=True` | ≥3 days of dry-run runs per broker with zero `error_summary`, diff sizes within expected bounds, reviewer signoff captured in runbook §Activation |
| **G3** (separate ops change) | First `--commit` run | G2 evidence + explicit reviewer approval in `docs/runbooks/execution-ledger.md` activation log |
| **G4** (separate PR) | Scheduler activation (Prefect unpause) | G3 evidence + 7 days of stable commit runs |

**Hard rules**:
- This PR must not include scheduler activation, environment-flag flip, or production migration runs.
- Direct SQL writes against `review.execution_ledger` are forbidden — enforced via repo-only convention and grep test in CI.
- `raw_payload_json` is sanitized through `_redact_sensitive_keys` (reuse market_events helper) before persistence.
- Secrets and API keys must not appear in `notes`, `error_summary`, or `raw_payload_json`.

---

## 7. Testing Strategy

| Layer | Test File | Coverage |
|---|---|---|
| Migration | `tests/migrations/test_rob211_execution_ledger.py` | upgrade → downgrade → upgrade, constraint enforcement |
| Repository | `tests/services/execution_ledger/test_repository.py` | idempotent upsert (insert/update/unchanged), conflict on `(broker, broker_order_id, fill_seq)` |
| Reconciler | `tests/services/execution_ledger/test_reconciler.py` | dry-run path, commit-flag gating, normalization, diff correctness |
| Reconciler (commit-disabled) | same file | asserts `ExecutionLedgerCommitDisabledError` when env flag missing |
| Query service | `tests/services/execution_ledger/test_query_service.py` | recent / by-symbol / sell-history filters; freshness state machine |
| Router | `tests/routers/test_invest_fills.py` | auth required; 4 GET endpoints; 200 / 404 / 401 cases |
| CLI | `tests/scripts/test_reconcile_execution_ledger.py` | `--dry-run` default; `--commit` rejected without env flag |
| Guard | `tests/services/execution_ledger/test_no_broker_mutation.py` | static-analysis grep: no `place_*_order`, `cancel_orders`, `order_overseas_stock` imports |

Use marker `@pytest.mark.integration` for the migration roundtrip and query-service tests (require real Postgres). Reconciler tests use mocked broker clients.

---

## 8. PR Strategy

**Recommendation: ONE narrow vertical PR.**

| Factor | Single PR | Split PRs |
|---|---|---|
| Migration is additive-only with no data | ✅ safe | unnecessary |
| Reconciler writes gated behind env flag | ✅ ships inert | inert in either case |
| Router is read-only | ✅ safe | unnecessary |
| Mirrors recent ROB-204 / ROB-207 / ROB-208 pattern | ✅ matches velocity | breaks pattern |
| File count (~12 new files + 1 migration + 4 test files + runbook) | manageable, focused | adds rebase churn |
| Independent reviewability of model vs. service vs. router | acceptable within one PR | only marginal benefit |

Single PR is preferred because the slice is internally cohesive (ledger is meaningless without reconciler; reconciler is unverifiable without the read endpoints) and every write path is behind a kill-switch env flag.

If reviewer pushes back on size during review, the natural split is:
1. **PR-A** — model + migration + repository + ORM tests (≈400 LOC)
2. **PR-B** — reconciler + CLI + TaskIQ task + reconciler tests (≈600 LOC)
3. **PR-C** — router + query service + router tests + runbook (≈400 LOC)

…in that order. Default to single PR; degrade to A/B/C only if requested.

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| KIS partial-fill `fill_seq` semantics differ between domestic / overseas | Document per-broker `fill_seq` derivation rules in `normalizers.py`; integration test covers both KIS surfaces |
| Upbit reports aggregated fills only — re-running same window over-updates | `unchanged` path in upsert + `fill_seq=0` convention; freshness based on `filled_at`, not aggregate volume |
| Reconciler accidentally promoted to commit mode in dev | `EXECUTION_LEDGER_COMMIT_ENABLED` defaults to `False`; `--commit` raises without it; documented in runbook |
| Normalization drift from `n8n_filled_orders_service` | Extract pure-function normalizers to `app/services/execution_ledger/normalizers.py` and have `n8n_filled_orders_service` import them — single source |
| Sensitive data leaks into `raw_payload_json` | Reuse `market_events` `_redact_sensitive_keys` helper; redaction unit-tested |
| Frontend integration delayed | Backend endpoints are stable contracts — frontend consumes in ROB-211 K1; no blocker for K0 ship |
| Migration ordering conflicts with concurrent PRs | Match existing date-prefixed convention (`2026_05_13_rob211_*`); alembic `down_revision` set to latest head at PR-open time |

---

## 10. Out-of-Scope Follow-ups

- **K1**: `/invest` frontend "Fills" tab on `RightAccountPanel` consuming `/recent` + `/freshness`.
- **K2**: WebSocket fill ingestion (would set `source='websocket'`, hook into existing KIS/Upbit WS monitors).
- **K3**: Alpaca live (non-paper) and Toss read-only manual import (separate ticket).
- **K4**: Cross-leg correlation against `watch-order-intent` (link via `correlation_id`).
- **K5**: Prefect scheduler deployment + unpause (separate PR in `robin-prefect-automations`).

---

## 11. References

- ROB-84 / ROB-90 `AlpacaPaperOrderLedger` — model & service pattern (`app/models/review.py:259`, `app/services/alpaca_paper_ledger_service.py`).
- ROB-119 `PendingOrder` — broker-canonical *open* orders (`app/models/pending_order.py:25`).
- ROB-128 Market Events — repository + freshness pattern (`app/services/market_events/`).
- ROB-204 / ROB-207 / ROB-208 — "ship paused / activate via runbook checklist" gating pattern.
- PR #800 / `52b6daba` — stabilized broker REST fill collection.
- `n8n_filled_orders_service.py` — existing normalization functions to reuse.
- CLAUDE.md "Worktree 운영 규칙" — branch & worktree conventions.
