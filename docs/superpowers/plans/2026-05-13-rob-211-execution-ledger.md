# ROB-211 K0 — Execution Ledger Vertical Slice (Implementation Plan)

**Spec:** [`docs/superpowers/specs/2026-05-13-rob-211-execution-ledger-design.md`](../specs/2026-05-13-rob-211-execution-ledger-design.md)
**Branch / Worktree:** `rob-211-execution-ledger` at `/Users/mgh3326/work/auto_trader/.worktrees/rob-211-execution-ledger`
**Base commit:** `52b6daba` (PR #800 merged)
**PR strategy:** ONE narrow vertical PR targeted at `main`. Degrade to A/B/C split only if reviewer requests.

## Approval gates (summary)

- **G0 — Merge gate (this PR):** alembic upgrade/downgrade green; all tests below pass; reviewer checklist confirms no broker mutation imports; runbook present.
- **G1 — Dry-run gate (post-merge, no code change):** ledger ships inert. Operator may run `--dry-run` against staging. No risk because no writes.
- **G2 — Commit-enable gate (separate ops change):** flip `EXECUTION_LEDGER_COMMIT_ENABLED=True` after ≥3 days of dry-run runs per broker with zero `error_summary` and diff sizes within bounds. Captured in runbook §Activation log with reviewer name.
- **G3 — First commit-run gate (operator action):** `scripts/reconcile_execution_ledger.py --broker {kis,upbit} --commit` executed once per broker by approved operator with paired reviewer. Output of first three commit runs archived in runbook.
- **G4 — Scheduler gate (separate PR in `robin-prefect-automations`):** Prefect deployment unpause after 7 days of stable commit runs. Out of scope for this PR.

This PR closes G0 only. G1–G4 are explicit, separate, sequential approvals.

---

## Task list

Tasks are ordered for sequential execution. Each task has a checkpoint that lets reviewers stop and verify before proceeding. Independent tasks are flagged `parallelizable`.

### Task 1 — Pydantic schemas (no DB, foundation)

**Files:**
- `app/schemas/execution_ledger.py` (new)

**Content:**
- `ExecutionLedgerUpsert` — input shape for `upsert_fill` (broker, account_mode, venue, instrument_type, symbol, raw_symbol, side, broker_order_id, fill_seq, filled_qty, filled_price, filled_notional, fee_amount, fee_currency, filled_at, currency, correlation_id, source, source_run_id, raw_payload_json).
- `ExecutionLedgerRead` — read-side projection (omits `raw_payload_json`).
- `ExecutionLedgerListResponse`, `ExecutionLedgerFreshnessReport`, `ExecutionLedgerFreshnessEntry`.
- `ReconcileDiff` — `{would_insert: int, would_update: int, unchanged: int, sample_inserts: list[ExecutionLedgerRead], sample_updates: list[ExecutionLedgerRead]}` (cap samples at 10).
- `ReconcileRunRecord` — for `execution_ledger_reconcile_runs` table.
- `ExecutionLedgerCommitDisabledError` — domain exception.

**Tests:**
- `tests/schemas/test_execution_ledger_schemas.py` — validation rules (broker enum, side enum, fill_seq ≥ 0, qty/price > 0).

**Checkpoint:** `uv run pytest tests/schemas/test_execution_ledger_schemas.py -v` green.

---

### Task 2 — ORM models

**Files:**
- `app/models/execution_ledger.py` (new) — `ExecutionLedger` + `ExecutionLedgerReconcileRun` classes (review schema, mirror `AlpacaPaperOrderLedger` styling).
- `app/models/__init__.py` (existing) — register new models so alembic autogenerate sees them.

**Notes:**
- CHECK constraints inline in `__table_args__`.
- `instrument_type` uses existing PG enum `instrument_type` with `create_type=False`.
- `__table_args__` ends with `{"schema": "review"}` (matches `AlpacaPaperOrderLedger`).

**Tests:**
- None beyond import-time check (models are exercised by migration test).

**Checkpoint:** `uv run python -c "from app.models.execution_ledger import ExecutionLedger, ExecutionLedgerReconcileRun"` succeeds.

---

### Task 3 — Alembic migration

**Files:**
- `alembic/versions/2026_05_13_rob211_add_execution_ledger.py` (new)

**Notes:**
- `revision = '<short_hash>'`, `down_revision = <current head>` (run `uv run alembic heads` at PR-open time).
- Creates `review.execution_ledger` and `review.execution_ledger_reconcile_runs` (table definitions per spec §4.1 / §4.2).
- Creates indexes: `uq_execution_ledger_broker_order_fill`, `ix_execution_ledger_filled_at`, `ix_execution_ledger_symbol_filled_at`, `ix_execution_ledger_broker_filled_at`, `ix_execution_ledger_source_run_id`.
- Downgrade drops both tables in reverse order.

**Tests:**
- `tests/migrations/test_rob211_execution_ledger.py` (new, `@pytest.mark.integration`):
  - Upgrade to head.
  - Verify tables exist with expected columns + constraints.
  - Downgrade one step.
  - Verify tables dropped.
  - Re-upgrade.

**Checkpoint:**
```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run pytest tests/migrations/test_rob211_execution_ledger.py -v
```

---

### Task 4 — Normalizer extraction (parallelizable with Task 5)

**Files:**
- `app/services/execution_ledger/__init__.py` (new, empty package marker)
- `app/services/execution_ledger/normalizers.py` (new)
- `app/services/n8n_filled_orders_service.py` (modify — import normalizers from new module)

**Content:**
- Move `_normalize_upbit_filled`, `_normalize_kis_domestic_filled`, `_normalize_kis_overseas_filled` from `n8n_filled_orders_service.py` to `normalizers.py` as pure functions.
- In `normalizers.py`, expose a new shape adapter: `to_execution_ledger_upsert(normalized: dict, *, broker, account_mode, source_run_id) -> ExecutionLedgerUpsert`.
- Add `fill_seq` derivation:
  - Upbit: always `0` (aggregate).
  - KIS domestic: from `ccld_seq` field if present, else hash of `(ord_dt, ord_tmd, ccld_qty)` truncated to int.
  - KIS overseas: from `ccld_seq` field if present, else `0`.
- Add `_redact_sensitive_keys(payload)` reused / imported from market_events helper. If not exposed, lift to `app/services/_redaction.py` (one-line import shim).
- Have `n8n_filled_orders_service.py` import the moved normalizers and re-export under existing names for backwards compatibility.

**Tests:**
- `tests/services/execution_ledger/test_normalizers.py` (new):
  - Upbit fill → expected upsert shape.
  - KIS domestic fill (with + without `ccld_seq`) → expected `fill_seq`.
  - KIS overseas fill → expected upsert shape.
  - Redaction strips API key / auth header substrings.

**Checkpoint:** `uv run pytest tests/services/execution_ledger/test_normalizers.py tests/services/test_n8n_filled_orders_service.py -v` green. (The second path catches regressions in the back-compat re-export.)

---

### Task 5 — Repository (parallelizable with Task 4)

**Files:**
- `app/services/execution_ledger/repository.py` (new)

**Content:**
- `ExecutionLedgerRepository(db: AsyncSession)` with:
  - `async upsert_fill(self, payload: ExecutionLedgerUpsert) -> tuple[Literal["inserted", "updated", "unchanged"], int]` — uses `insert(...).on_conflict_do_update(...)` on `(broker, broker_order_id, fill_seq)`. Determine `unchanged` by comparing pre-image to payload **before** issuing UPDATE (read-then-conditional-upsert is acceptable; favor explicit pre-check for clarity).
  - `async record_run(self, run: ReconcileRunRecord) -> None`.
  - `async latest_run_per_broker(self) -> dict[str, ReconcileRunRecord]`.
  - `async list_recent(self, *, limit: int, market: str | None) -> list[ExecutionLedger]`.
  - `async list_by_symbol(self, *, symbol: str, days: int) -> list[ExecutionLedger]`.
  - `async list_sell_history(self, *, days: int, market: str | None) -> list[ExecutionLedger]`.

**Notes:**
- `market` → predicate mapping: `kr → broker='kis' AND venue ILIKE 'kr%'`; `us → broker='kis' AND venue IN ('NASD','NYSE','AMEX')`; `crypto → broker='upbit'`.
- All `INSERT`/`UPDATE` statements live ONLY in this file. (Verified by guard test in Task 10.)

**Tests:**
- `tests/services/execution_ledger/test_repository.py` (new, `@pytest.mark.integration`):
  - Insert new fill → `inserted`.
  - Re-insert identical → `unchanged`.
  - Re-insert with updated price → `updated`.
  - Filter by symbol / market / days returns correct subset.

**Checkpoint:** `uv run pytest tests/services/execution_ledger/test_repository.py -v` green.

---

### Task 6 — Reconciler

**Files:**
- `app/services/execution_ledger/reconciler.py` (new)
- `app/core/settings.py` (modify — add `EXECUTION_LEDGER_COMMIT_ENABLED: bool = False`)

**Content:**
- `class BrokerHistoryClients` — small adapter wrapping `KISClient` + Upbit `orders` module for testability. Constructor injection.
- `class ExecutionLedgerReconciler`:
  - `__init__(self, repo, brokers, settings)`.
  - `async run(self, broker, *, window_hours=24, dry_run=True) -> ReconcileDiff`:
    1. Compute window (`window_end = now`, `window_start = now - window_hours`).
    2. Allocate `source_run_id = uuid4()`.
    3. Fetch fills via broker.
    4. For each fill: normalize → adapt to `ExecutionLedgerUpsert`.
    5. For each upsert payload: classify against current DB row (read-only check) → accumulate diff.
    6. If `dry_run=True`: skip writes; `record_run(dry_run=True, would_*=...)`.
    7. If `dry_run=False`: assert `settings.EXECUTION_LEDGER_COMMIT_ENABLED is True` (raise `ExecutionLedgerCommitDisabledError` otherwise); call `upsert_fill` for each diff row; record_run with committed counts.
- Logging: structured log lines with `source_run_id`, `broker`, `window_hours`, diff counts. No raw payload in logs.

**Tests:**
- `tests/services/execution_ledger/test_reconciler.py` (new):
  - Dry-run on empty DB → all `would_insert`.
  - Dry-run on pre-seeded DB → mix of `would_update` / `unchanged`.
  - `dry_run=False` without env flag → raises `ExecutionLedgerCommitDisabledError`.
  - `dry_run=False` with env flag → calls repository `upsert_fill` (mocked count assertion).
  - Records reconcile-run row in both modes.

**Checkpoint:** `uv run pytest tests/services/execution_ledger/test_reconciler.py -v` green.

---

### Task 7 — Query service

**Files:**
- `app/services/execution_ledger/query_service.py` (new)

**Content:**
- `ExecutionLedgerQueryService(db)` wrapping `ExecutionLedgerRepository` reads.
- `freshness()` computes `dataState` per broker:
  - threshold = `2 * default_window_hours` (default 24 → fresh-window = 48h). Configurable later.
  - `missing` → no `ReconcileRunRecord` for broker.
  - `fresh` → `now - last_success_finished_at <= 48h`.
  - `stale` → between 48h and 72h.
  - `missing` again past 72h (treated as `stale` with explicit `notes='exceeded staleness threshold'`).

**Tests:**
- `tests/services/execution_ledger/test_query_service.py` (new, integration):
  - Seeds runs with varying `finished_at` and asserts `dataState`.
  - `list_recent` pagination + ordering.
  - `list_sell_history` filters `side='sell'`.

**Checkpoint:** `uv run pytest tests/services/execution_ledger/test_query_service.py -v` green.

---

### Task 8 — Router

**Files:**
- `app/routers/invest_fills.py` (new)
- `app/main.py` (modify — `include_router(invest_fills.router)`)

**Endpoints:**
- `GET /trading/api/invest/fills/recent` → `ExecutionLedgerListResponse`.
- `GET /trading/api/invest/fills/by-symbol/{symbol}` → `ExecutionLedgerListResponse`.
- `GET /trading/api/invest/fills/sell-history` → `ExecutionLedgerListResponse`.
- `GET /trading/api/invest/fills/freshness` → `ExecutionLedgerFreshnessReport`.

All require `Depends(get_authenticated_user)`. No POST/PATCH/DELETE.

**Tests:**
- `tests/routers/test_invest_fills.py` (new):
  - Unauthenticated → 401.
  - Authenticated + empty DB → empty list + freshness `missing`.
  - Authenticated + seeded DB → correct items.
  - Unknown symbol path → 200 + empty list (not 404 — list endpoint).

**Checkpoint:** `uv run pytest tests/routers/test_invest_fills.py -v` green.

---

### Task 9 — CLI + TaskIQ scheduleless task

**Files:**
- `scripts/reconcile_execution_ledger.py` (new)
- `app/tasks/execution_ledger.py` (new) — TaskIQ task wrapping reconciler.run, defined but NOT registered into any recurring schedule.

**CLI flags:**
- `--broker {kis,upbit}` (required)
- `--window-hours INT` (default 24)
- `--dry-run` / `--commit` (mutually exclusive; default `--dry-run`)
- `--source-run-id UUID` (optional override for re-running a window)

**Behavior:**
- `--commit` without `EXECUTION_LEDGER_COMMIT_ENABLED=True` → exit code 2 + clear error.
- Prints `ReconcileDiff` as JSON + table summary.

**Tests:**
- `tests/scripts/test_reconcile_execution_ledger.py` (new) — uses `typer.testing.CliRunner` or argparse subprocess:
  - Default invocation = dry-run.
  - `--commit` without env flag → exit 2.
  - `--commit` with env flag → calls reconciler.

**Checkpoint:** `uv run pytest tests/scripts/test_reconcile_execution_ledger.py -v` green.

---

### Task 10 — Safety guard tests

**Files:**
- `tests/services/execution_ledger/test_no_broker_mutation.py` (new)
- `tests/services/execution_ledger/test_repository_only_writes.py` (new)

**Content:**
- `test_no_broker_mutation.py` — grep across `app/services/execution_ledger/` for forbidden symbols: `place_buy_order`, `place_sell_order`, `cancel_orders`, `order_overseas_stock`, `modify_*_order`. Fail if any matched.
- `test_repository_only_writes.py` — grep across `app/services/execution_ledger/` (excluding `repository.py`) for `INSERT`, `UPDATE`, `DELETE`, `.add(`, `.delete(`, `.merge(`, `.flush(` on a `ExecutionLedger*` model. Fail if any matched.

**Checkpoint:** `uv run pytest tests/services/execution_ledger/test_no_broker_mutation.py tests/services/execution_ledger/test_repository_only_writes.py -v` green.

---

### Task 11 — Runbook

**Files:**
- `docs/runbooks/execution-ledger.md` (new)

**Sections:**
1. Purpose + scope (read-only; KIS + Upbit live; idempotent).
2. Tables (`review.execution_ledger`, `review.execution_ledger_reconcile_runs`) — columns, constraints, indexes.
3. Service surfaces (repository, reconciler, query service, router, CLI).
4. CLI reference + examples (dry-run, --commit gating).
5. Read endpoints — `curl` examples and response shapes.
6. **Activation checklist** (Gates G1–G4):
   - G1 Dry-run period — log of `--dry-run` runs (3+ days per broker, signed off).
   - G2 Env flag flip — when to set `EXECUTION_LEDGER_COMMIT_ENABLED=True`, who approves.
   - G3 First commit run — operator + reviewer pair, output archived.
   - G4 Scheduler unpause — link to robin-prefect-automations PR.
7. Troubleshooting — what `error_summary` values mean, how to re-run a window.
8. Safety guarantees (no broker mutation, repository-only writes, redaction).

**Checkpoint:** Reviewer reads and signs off the runbook in PR review.

---

### Task 12 — CLAUDE.md update

**Files:**
- `CLAUDE.md` (modify) — add an "Execution Ledger (ROB-211)" section in the same style as the Alpaca Paper, ROB-128 Market Events, and ROB-140 Research Reports sections.

**Content:**
- Model / repository / router / CLI / runbook paths.
- Safety boundaries.
- Activation gates.

**Checkpoint:** included in single PR.

---

### Task 13 — PR open

**Steps:**
1. `uv run alembic upgrade head` against ephemeral test DB — green.
2. `make lint && make typecheck` — green.
3. `make test` — green (or scoped: `pytest tests/services/execution_ledger tests/routers/test_invest_fills.py tests/migrations/test_rob211_execution_ledger.py tests/schemas/test_execution_ledger_schemas.py tests/scripts/test_reconcile_execution_ledger.py -v`).
4. `git push -u origin rob-211-execution-ledger`.
5. Open PR with:
   - Base: `main`
   - Title: `feat(ROB-211): add execution ledger + dry-run reconciliation foundation`
   - Body: links to spec + this plan, summarizes acceptance criteria, lists G0 evidence (CI green, no broker mutation, runbook present), explicitly notes scheduler is paused / commit is disabled.

---

## Estimated file inventory

| Kind | Path | New / Modified |
|---|---|---|
| Spec | `docs/superpowers/specs/2026-05-13-rob-211-execution-ledger-design.md` | new (this PR's planning artifact) |
| Plan | `docs/superpowers/plans/2026-05-13-rob-211-execution-ledger.md` | new (this PR's planning artifact) |
| Schema | `app/schemas/execution_ledger.py` | new |
| Model | `app/models/execution_ledger.py` | new |
| Model registry | `app/models/__init__.py` | modified |
| Migration | `alembic/versions/2026_05_13_rob211_add_execution_ledger.py` | new |
| Service pkg | `app/services/execution_ledger/__init__.py` | new |
| Service | `app/services/execution_ledger/normalizers.py` | new |
| Service | `app/services/execution_ledger/repository.py` | new |
| Service | `app/services/execution_ledger/reconciler.py` | new |
| Service | `app/services/execution_ledger/query_service.py` | new |
| Service refactor | `app/services/n8n_filled_orders_service.py` | modified (re-export) |
| Settings | `app/core/settings.py` | modified (new flag) |
| Router | `app/routers/invest_fills.py` | new |
| Router mount | `app/main.py` | modified |
| CLI | `scripts/reconcile_execution_ledger.py` | new |
| Task | `app/tasks/execution_ledger.py` | new |
| Runbook | `docs/runbooks/execution-ledger.md` | new |
| Docs | `CLAUDE.md` | modified |
| Tests (8 new files) | `tests/schemas/`, `tests/services/execution_ledger/`, `tests/routers/`, `tests/migrations/`, `tests/scripts/` | new |

**Total**: ≈19 production files + 8 test files + 2 planning artifacts. Single PR is the right shape.

---

## Definition of done (G0)

- [ ] All 13 tasks above complete with checkpoint commands green.
- [ ] `make lint`, `make typecheck`, `make test` all green on CI.
- [ ] Reviewer confirms no broker mutation imports under `app/services/execution_ledger/` (manual + `test_no_broker_mutation`).
- [ ] Reviewer confirms `EXECUTION_LEDGER_COMMIT_ENABLED` defaults to `False` and `--commit` is unreachable without it.
- [ ] Runbook reviewed and merged.
- [ ] PR description clearly states: ships inert, requires G1–G4 to activate.
- [ ] No changes to `robin-prefect-automations` in this PR.

---

## What this PR explicitly does NOT do

- Does not write any row to `review.execution_ledger` in production.
- Does not enable the TaskIQ task on any recurring schedule.
- Does not modify any broker order submit / cancel / modify code path.
- Does not backfill historical fills.
- Does not change `review.trades`, `review.alpaca_paper_order_ledger`, `review.kis_mock_order_ledger`, `pending_orders`, or any existing model.
- Does not add `/invest` frontend changes (deferred to K1).
