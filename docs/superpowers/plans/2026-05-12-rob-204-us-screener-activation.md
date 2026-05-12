# ROB-204 — US Screener Snapshot Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the `invest_screener_snapshots` US write path so `/invest/screener?market=us&preset=consecutive_gainers` serves snapshot-backed rows (rather than the current `dataState="missing"` 0-row response), while keeping production writes behind an explicit operator/reviewer approval gate. Add a bounded **common-stock filter** so the first US commit is scoped to a tractable subset of the ~12,238-active universe. Surface `dataState` to the React UI so users see when results are missing/stale. Land a Prefect flow for the post-US-close recurring refresh, but do **not** register the deployment in this PR.

**Architecture:** Reuse the ROB-170 snapshot foundation (model, repository, builder, freshness, coverage service, view-model snapshot-first wiring) and the ROB-204 PR #793 dry-run seam (`run_snapshot_build`, CLI, TaskIQ wrapper) unchanged. Add (1) an additive `is_common_stock` column on `us_symbol_universe` populated from NASDAQ Trader's `nasdaqlisted.txt` + `otherlisted.txt`, (2) a `--common-stocks-only` flag on the resolver / CLI / TaskIQ task so the first US activation is scoped to ~3–4K common stocks, (3) a user-facing missing-snapshot warning string emitted by `build_screener_results` for US, (4) a `dataState` field on the frontend `ScreenerFreshness` type and a chip in `ScreenerFreshnessLine` for the non-fresh states, (5) a Prefect flow `invest_screener_snapshots_us_flow` (importable but unregistered, gated by `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` env flag), and (6) runbook + CLAUDE.md updates capturing the activation evidence sequence. No broker / order / watch / order-intent mutations.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, PostgreSQL with JSONB, Alembic, TaskIQ (Redis ListQueueBroker, LabelScheduleSource — used only for the dry-run smoke task; not for the recurring schedule), Prefect (deployment deferred), Pydantic v2, React + Vitest + Testing Library.

**Model handoff note:** Per the K5 task body, planner/reviewer should use Claude Code Opus; implementer should use Claude Code Sonnet. Record the actual model used at each step in the Kanban handoff metadata. This plan was authored on Claude Opus 4.7 from a Hermes Kanban worker (executor: Claude Code).

---

## Worktree / Base-branch Note (Important)

The Kanban worktree was created from `origin/main` = `82fca64c` ("feat(invest): add persistent right rail panel (#734)"). That commit predates the ROB-170 / ROB-202 / ROB-203 / ROB-204 PR #793 snapshot foundation, so the files this plan modifies (e.g., `app/jobs/invest_screener_snapshots.py`, `app/services/invest_screener_snapshots/*`) do **not** exist in the worktree's working tree.

The production / current pipeline at `308f33fe` (and at the ROB-204 PR #793 commit `6024a264`) contains the foundation. Before starting implementation:

1. The implementer **must** rebase the kanban branch onto the production-current commit that contains PR #793 (i.e., a commit that has `app/services/invest_screener_snapshots/builder.py` and the `82309c07b8a2_add_invest_screener_snapshots.py` migration). If PR #793 is still open against `main`, base the implementation branch on it directly. Confirm via:

```bash
ls app/services/invest_screener_snapshots/builder.py \
   app/jobs/invest_screener_snapshots.py \
   alembic/versions/82309c07b8a2_add_invest_screener_snapshots.py
```

All three must exist before Task 1.

2. The implementer **must not** edit shared `/Users/mgh3326/work/auto_trader` or the production checkout. All work stays in the worktree at `/Users/mgh3326/worktrees/auto_trader/rob-204-us-screener-activation`.

3. If the rebase produces conflicts, prefer surgical resolution that keeps PR #793's seam intact; do not rewrite the snapshot foundation.

---

## Pre-conditions / Reference reading

Before starting Task 1, read (paths refer to the rebased branch with PR #793 applied):

- `app/jobs/invest_screener_snapshots.py` — `SnapshotBuildRequest`, `SnapshotBuildResult`, `resolve_symbols`, `resolve_active_universe`, `run_snapshot_build`. This is the boundary we extend.
- `app/services/invest_screener_snapshots/builder.py` — `build_snapshots_for_market`, `build_snapshot_for_symbol`, `derive_metrics`. Reused unchanged.
- `app/services/invest_screener_snapshots/repository.py` — `InvestScreenerSnapshotsRepository.upsert` (the **only** write path). Do not introduce alternative writers.
- `app/services/invest_screener_snapshots/coverage_service.py` — `build_coverage` (read-only).
- `app/services/invest_screener_snapshots/freshness.py` — `classify_state`, `aggregate_states`, `today_trading_date`.
- `app/services/invest_view_model/screener_service.py` — `build_screener_results`, `_load_consecutive_gainers_from_snapshots`, `_aggregated_data_state`. We add a user-facing warning when `dataState ∈ {"missing", "stale"}` for `requested_market=="us"`.
- `app/services/invest_view_model/screener_presets.py` — `screening_filters_for` (already supports `market="us"`).
- `app/schemas/invest_screener.py` — `ScreenerFreshness.dataState` is already on the Python side; the frontend type needs the same field.
- `app/services/us_symbol_universe_service.py` — current KIS COD-driven sync. We add an additive NASDAQ-Trader-driven classifier; do not change the COD sync semantics.
- `app/models/us_symbol_universe.py` — schema; we add `is_common_stock` as an additive nullable column.
- `app/tasks/invest_screener_snapshot_tasks.py` — TaskIQ wrapper; we extend the signature.
- `scripts/build_invest_screener_snapshots.py` — operator CLI; we extend it.
- `scripts/diagnose_invest_screener_snapshots.py` — read-only diagnostic; unchanged.
- `docs/runbooks/invest-screener-snapshots.md` — runbook; we extend §2 and §5.
- `app/flows/forexfactory_calendar_flow.py` — Prefect flow template (importable, deployment deferred). Mirror this pattern.
- `app/core/config.py` — Settings module. We add `invest_screener_snapshots_commit_enabled` (default `False`).
- `frontend/invest/src/types/screener.ts` — frontend `ScreenerFreshness` type; we add `dataState`.
- `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — display component; we add a chip for `dataState ∈ {"missing", "stale", "fallback"}`.
- `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx` — extend tests.
- `frontend/invest/src/pages/desktop/DesktopScreenerPage.tsx` — calls `fetchScreenerResults(selectedId, selectedMarket)`. No change required if the freshness component handles the new field.

---

## Approval Gates (restated; ALL required for production activation)

The planner (this task) exercises **none** of these. The implementer (K1–K3) does not exercise the gates marked "operator only". The K5 reviewer / handoff calls these gates out in the PR body so they are explicit at merge time.

1. **No production DB write in CI / local dev / staging.** Tests must use either an in-memory async session, a transactional fixture, or explicit dry-run mode. The `--commit` flag may only be used in production by an operator after an approval round.
2. **`INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` defaults to `False`** in `app/core/config.py`. The Prefect flow body checks this flag and short-circuits to dry-run when `False`. Recurring-schedule activation = flipping this flag on the deployed Prefect worker only after operator/reviewer approval. There is no compile-time toggle of flow registration; the deployment is **not registered** in this PR.
3. **First US commit must be bounded.** The first production `--commit` invocation must specify `--common-stocks-only` (after Task 2 lands the column population) so only ~3–4K rows are written. After 24–48h of stable coverage, a second approval round expands to the full universe if desired. (See §Operational activation procedure below.)
4. **Common-stock filter requires a populated `is_common_stock` column.** Task 1 ships the migration, Task 2 ships the populator and the operator must run `scripts/sync_us_common_stock_flags.py` once (read-only HTTP fetch, single DB write transaction) before Task 3's filter is meaningful. The CLI must reject `--common-stocks-only` with a clear error if no rows have `is_common_stock IS NOT NULL`.
5. **Prefect deployment registration is out of scope.** This PR lands the flow as an importable module, *not* a registered deployment. Deployment registration (cron `30 21 * * 1-5` UTC ≈ 17:30 ET, ~30 min after the regular US close) is a separate operator action after first-commit evidence is captured.
6. **TaskIQ cron is not used for the recurring refresh.** TaskIQ wraps the operator-controlled dry-run task only. Per the K5 task body, "Prefer Prefect for recurring freshness; Hermes cron only as a short bridge if explicitly approved" — and no Hermes-cron bridge is part of this PR.
7. **No broker / order / watch / order-intent / paper-trading mutation** anywhere in this issue. All snapshot writes go through `InvestScreenerSnapshotsRepository.upsert`. The view-model and frontend remain read-only.
8. **Sensitive content handling:** NASDAQ Trader files are public; no secrets. The Prefect flow body must not print env values. Do not log full row payloads from the new flow at INFO; INFO logs aggregate counts only.

Each implementer task below restates the relevant gate in its acceptance criteria.

---

## File Structure

**Create:**

- `alembic/versions/<rev>_add_us_symbol_universe_is_common_stock.py` — additive migration adding `is_common_stock` nullable column + partial index.
- `app/services/us_common_stock_classifier.py` — fetch + parse NASDAQ Trader `nasdaqlisted.txt` and `otherlisted.txt`; classify each symbol; expose `sync_us_common_stock_flags()` writer (uses `USSymbolUniverse` via existing async session).
- `app/jobs/us_common_stock_classifier.py` — thin async wrapper around `sync_us_common_stock_flags()` for symmetry with `app/jobs/us_symbol_universe.py`.
- `scripts/sync_us_common_stock_flags.py` — operator CLI (dry-run by default; `--commit` to persist).
- `tests/test_us_common_stock_classifier.py` — unit tests for the classifier helpers.
- `tests/test_sync_us_common_stock_flags_cli.py` — CLI parsing + dry-run/commit gate tests.
- `app/flows/invest_screener_snapshots_us_flow.py` — Prefect `@flow`/`@task` wrapper around `run_snapshot_build` (gated by env flag, dry-run by default).
- `tests/test_invest_screener_snapshots_us_flow.py` — flow body tests (no Prefect server needed; call the underlying coroutine).
- `tests/test_screener_us_missing_warning.py` — view-model test that asserts the user-facing warning string is emitted when `dataState in {"missing", "stale"}` for `market=="us"`.

**Modify:**

- `app/models/us_symbol_universe.py` — add `is_common_stock: Mapped[bool | None]`.
- `app/jobs/invest_screener_snapshots.py` — extend `SnapshotBuildRequest` with `common_stocks_only: bool = False`; extend `resolve_symbols` / `resolve_active_universe` to apply the filter when set; raise `ValueError` if the flag is set against an empty `is_common_stock` column.
- `app/tasks/invest_screener_snapshot_tasks.py` — add `common_stocks_only: bool = False` parameter; forward into `SnapshotBuildRequest`.
- `scripts/build_invest_screener_snapshots.py` — add `--common-stocks-only` flag (mutually compatible with `--all` and `--symbol`; raises if used with `--market kr` for now).
- `app/services/invest_view_model/screener_service.py` — when `requested_market == "us"` and `_aggregated_data_state in {"missing", "stale"}`, append a localized user-facing warning string (e.g., `"미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."`) to `upstream_warnings` before constructing the response.
- `app/core/config.py` — add `invest_screener_snapshots_commit_enabled: bool = False` setting (env: `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`).
- `tests/test_invest_screener_snapshots_builder.py` (if it exists; otherwise extend `tests/test_build_invest_screener_snapshots_full_universe.py`) — add a `common_stocks_only=True` case.
- `tests/test_build_invest_screener_snapshots_cli.py` — assert the new flag is parsed and threaded into `SnapshotBuildRequest`.
- `tests/test_invest_screener_snapshot_tasks.py` — assert the new TaskIQ parameter is forwarded.
- `frontend/invest/src/types/screener.ts` — add `dataState: "fresh" | "partial" | "stale" | "missing" | "fallback"` (default "missing" on the Python side; the frontend may treat missing field as legacy and default to "fresh" for backward compatibility, but the new field is required for new builds).
- `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` — render a chip (`<span className="screener-freshness-state screener-freshness-state--{state}">…</span>`) for `dataState ∈ {"missing", "stale", "fallback", "partial"}` with localized text. `fresh` renders no chip.
- `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx` — add tests for each chip state.
- `frontend/invest/src/desktop/screener/screener.css` — add `.screener-freshness-state*` styles.
- `docs/runbooks/invest-screener-snapshots.md` — append §7 "US activation procedure" and §8 "Prefect deployment (deferred)".
- `CLAUDE.md` — append ROB-204 entry under the ROB-170 section, referencing the new common-stock classifier, the user-facing warning, and the Prefect flow (deployment deferred).

**Do NOT modify:**

- `app/models/invest_screener_snapshot.py` (no schema change to the snapshot table)
- `app/services/invest_screener_snapshots/repository.py` (writes still go through `upsert` only)
- `app/services/invest_screener_snapshots/builder.py` (unchanged)
- Any broker / order / watch / order-intent / paper-trading code paths
- `app/services/us_symbol_universe_service.py` core sync logic (we add an *additional*, additive classifier; we do not change the COD sync)
- `production` checkout, `shared/current`, or `/Users/mgh3326/work/auto_trader` outside this worktree

---

## Operational Activation Procedure (DOCUMENTATION ONLY; do NOT execute in this PR)

This section captures the exact operator commands. They are referenced by the PR description as the activation runbook the reviewer will execute *after* merge.

### Phase 0 — Pre-flight (read-only)

```bash
# Baseline US coverage diagnostic — confirm dataState=missing, universe count
uv run python -m scripts.diagnose_invest_screener_snapshots --market us

# HTTP equivalent (production)
curl -fsS "$INVEST_BASE_URL/invest/api/screener/snapshots/coverage?market=us"

# Confirm US universe size and sample
psql "$DATABASE_URL" -c "SELECT count(*) FROM us_symbol_universe WHERE is_active = true;"
psql "$DATABASE_URL" -c "SELECT count(*) FROM us_symbol_universe WHERE is_active = true AND is_common_stock IS TRUE;"
```

### Phase 1 — Populate `is_common_stock` (one-time, read-mostly)

```bash
# Dry-run: print row delta proposed
uv run python -m scripts.sync_us_common_stock_flags

# Commit (requires operator approval — single transaction, additive column only)
uv run python -m scripts.sync_us_common_stock_flags --commit
```

Expected: ~3,000–4,000 rows flipped to `is_common_stock=true`; ~7,000–9,000 rows flipped to `is_common_stock=false` (ETFs, ADRs in some categories, warrants, rights, units, preferreds, test issues). Inactive symbols left as `NULL`.

### Phase 2 — Bounded US dry-run (no DB writes)

```bash
# Common-stocks-only, dry-run, full active common-stock universe
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only

# Smaller sampled dry-run (first 50 common stocks, for quick smoke)
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --common-stocks-only --limit 50
```

Capture stdout to the approval packet. Expected: snapshot_date distribution clustered on the most recent US trading date, warnings list small (<5% of symbols), no exceptions raised.

### Phase 3 — Reviewer approval round

Post the dry-run summary to Linear ROB-204 with the following template:

```
US screener snapshot dry-run evidence (ROB-204, Phase 2):

- symbols_resolved: <N>
- snapshots_built: <M>
- skipped: <S>  (~<S/N*100>% — investigate if >10%)
- snapshot_date_distribution: <date>: <count>, ...
- batches: <B>
- warnings:
  - <truncated warning sample, ≤5 lines>
- samples: <first 10 rows of (symbol, snapshot_date, latest_close, consecutive_up_days, week_change_rate)>

Requesting approval to run `--commit` on the same scope.
```

Wait for an explicit "approved to commit" reply from a reviewer citing the dry-run evidence above.

### Phase 4 — Bounded US commit

```bash
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only --commit

# Re-check coverage
uv run python -m scripts.diagnose_invest_screener_snapshots --market us
curl -fsS "$INVEST_BASE_URL/invest/api/screener/snapshots/coverage?market=us"
```

Expected: `dataState="fresh"` (or `"partial"` if many symbols have <5 closes available).

### Phase 5 — Spot-check `/invest/screener?market=us` in production

- UI: open `/invest/screener`, toggle 미국, confirm `consecutive_gainers` returns >0 rows; confirm freshness chip is absent (state=fresh) or shows the right copy.
- API: `curl -fsS "$INVEST_BASE_URL/invest/api/screener/results?preset=consecutive_gainers&market=us"` — confirm non-empty `results[]` and `freshness.dataState="fresh"`.

### Phase 6 — Prefect deployment (DEFERRED; separate ticket)

This PR ships the flow as an importable module. Registration is a separate operator action gated on Phase 4–5 stability across at least 24 hours and an explicit reviewer approval. The intended schedule is `30 21 * * 1-5` (UTC; ≈17:30 America/New_York, ~30 min after the regular US session close, late enough to let daily candle data finalize). The flow body honors `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`; if `False`, it runs dry-run and emits the same `SnapshotBuildResult` for inspection.

```bash
# When (and only when) approved:
export INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=true
prefect deployment apply path/to/invest-screener-snapshots-us-deployment.yaml
# Trigger smoke run
prefect deployment run 'invest_screener_snapshots_us/post-us-close'
```

---

## Data Model Decisions

Single additive column on `us_symbol_universe`. No new tables. No changes to `invest_screener_snapshots`.

### `us_symbol_universe.is_common_stock`

```sql
ALTER TABLE us_symbol_universe ADD COLUMN is_common_stock BOOLEAN NULL;
CREATE INDEX ix_us_symbol_universe_active_common_stock
  ON us_symbol_universe (is_active, is_common_stock)
  WHERE is_active = true AND is_common_stock IS TRUE;
```

- `NULL` = unclassified (default for existing rows after migration; default for any new COD-only sync until the classifier runs).
- `TRUE` = listed as a non-ETF, non-test, non-warrant/right/unit/preferred common-equity symbol on NASDAQ/NYSE/AMEX in NASDAQ Trader's `nasdaqlisted.txt` or `otherlisted.txt`.
- `FALSE` = classified as a non-common (ETF=Y, Test Issue=Y, or pattern-matched suffix indicating warrant/right/unit/preferred).

The filter expression in the universe resolver is `WHERE is_active = true AND is_common_stock IS TRUE`. `NULL` is intentionally excluded (treat unknown as not-common) so accidental partial classification cannot mass-include unclassified rows.

### Classifier rules (deterministic, idempotent)

For each row in `nasdaqlisted.txt | otherlisted.txt`:

1. If `Test Issue == "Y"` → `is_common_stock = False`.
2. Else if `ETF == "Y"` → `is_common_stock = False`.
3. Else if the symbol contains `^` or matches `*-W$|*-R$|*-U$|*\.P$|*\.PR.$|*\.WS$` → `is_common_stock = False` (warrants, rights, units, preferreds).
4. Else → `is_common_stock = True`.

Symbols present in `us_symbol_universe` but absent from both NASDAQ Trader files are left at `NULL` (foreign listings, OTC pinks, etc.).

This is conservative and easy to reason about; later tickets can refine with sector / market-cap / liquidity gates.

---

## Task 1: Add `is_common_stock` column to `us_symbol_universe`

**Goal:** Land the additive column + partial index. No data is written by this task; values stay `NULL` until Task 2's sync runs.

**Files:**
- Create: `alembic/versions/<auto>_add_us_symbol_universe_is_common_stock.py`
- Modify: `app/models/us_symbol_universe.py`
- Test: existing `tests/test_us_symbol_universe_sync.py` plus a new `tests/test_us_symbol_universe_model.py` if not present.

**Acceptance:** Migration is additive (no `ALTER … NOT NULL`, no data write). The model column is nullable. The partial index uses `is_active = true AND is_common_stock IS TRUE`. Downgrade drops the index then the column. No broker / order code touched.

### Step 1.1 — Write the failing model test

- [ ] **Write the failing test** in `tests/test_us_symbol_universe_model.py` (create if absent):

```python
import pytest
from sqlalchemy import inspect

from app.models.us_symbol_universe import USSymbolUniverse


@pytest.mark.unit
def test_us_symbol_universe_has_is_common_stock_nullable() -> None:
    table = USSymbolUniverse.__table__
    column = table.columns.get("is_common_stock")
    assert column is not None, "is_common_stock column missing"
    assert column.nullable is True, "is_common_stock must be nullable"
    assert column.type.python_type is bool


@pytest.mark.unit
def test_us_symbol_universe_has_active_common_stock_partial_index() -> None:
    indexes = USSymbolUniverse.__table__.indexes
    target = next(
        (i for i in indexes if i.name == "ix_us_symbol_universe_active_common_stock"),
        None,
    )
    assert target is not None, "expected partial index missing"
    # SQLAlchemy stores the postgresql_where on dialect options
    assert "is_common_stock IS TRUE" in str(target.dialect_options.get("postgresql", {}).get("where", ""))
```

- [ ] **Run the test to verify it fails**

```bash
uv run pytest tests/test_us_symbol_universe_model.py -v -k is_common_stock
```

Expected: FAIL with `AssertionError: is_common_stock column missing`.

### Step 1.2 — Add the column to the model

- [ ] **Edit** `app/models/us_symbol_universe.py`. Add the column after `is_active`:

```python
from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
# ...
class USSymbolUniverse(Base):
    __tablename__ = "us_symbol_universe"
    __table_args__ = (
        Index(
            "ix_us_symbol_universe_exchange_is_active",
            "exchange",
            "is_active",
        ),
        Index(
            "ix_us_symbol_universe_active_common_stock",
            "is_active",
            "is_common_stock",
            postgresql_where=(
                "is_active = true AND is_common_stock IS TRUE"
            ),
        ),
    )
    # ... existing columns ...
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_common_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ... created_at / updated_at unchanged ...
```

- [ ] **Run the model tests** — they now pass.

```bash
uv run pytest tests/test_us_symbol_universe_model.py -v
```

Expected: PASS.

### Step 1.3 — Generate and harden the Alembic migration

- [ ] **Generate** the migration:

```bash
uv run alembic revision --autogenerate -m "add us_symbol_universe is_common_stock"
```

- [ ] **Open** the generated `alembic/versions/<rev>_add_us_symbol_universe_is_common_stock.py`. Replace the autogenerated body with:

```python
"""add us_symbol_universe is_common_stock

Revision ID: <auto>
Revises: 82309c07b8a2
Create Date: 2026-05-12 …

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "<auto>"
down_revision: Union[str, Sequence[str], None] = "82309c07b8a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "us_symbol_universe",
        sa.Column("is_common_stock", sa.Boolean(), nullable=True),
    )
    op.create_index(
        "ix_us_symbol_universe_active_common_stock",
        "us_symbol_universe",
        ["is_active", "is_common_stock"],
        unique=False,
        postgresql_where=sa.text("is_active = true AND is_common_stock IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_us_symbol_universe_active_common_stock",
        table_name="us_symbol_universe",
        postgresql_where=sa.text("is_active = true AND is_common_stock IS TRUE"),
    )
    op.drop_column("us_symbol_universe", "is_common_stock")
```

Replace `<auto>` and the `down_revision` to match the alembic head on the rebase target. Confirm `Revises` points to the snapshot foundation migration (`82309c07b8a2_add_invest_screener_snapshots`) or its successor; **the head must be the latest version present on the rebase base**.

- [ ] **Apply the migration locally** against a disposable database:

```bash
uv run alembic upgrade head
uv run alembic current   # confirms the new head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: clean up/down/up cycle with no errors.

### Step 1.4 — Commit

```bash
git add app/models/us_symbol_universe.py \
        alembic/versions/<auto>_add_us_symbol_universe_is_common_stock.py \
        tests/test_us_symbol_universe_model.py
git commit -m "feat(ROB-204): add us_symbol_universe.is_common_stock column"
```

---

## Task 2: NASDAQ Trader classifier + sync CLI

**Goal:** Fetch `nasdaqlisted.txt` and `otherlisted.txt`, classify each row by the rules above, and update `is_common_stock` in a single transaction (dry-run by default, `--commit` to persist).

**Files:**
- Create: `app/services/us_common_stock_classifier.py`
- Create: `app/jobs/us_common_stock_classifier.py`
- Create: `scripts/sync_us_common_stock_flags.py`
- Create: `tests/test_us_common_stock_classifier.py`
- Create: `tests/test_sync_us_common_stock_flags_cli.py`

**Acceptance:** Pure parsing helpers have zero I/O. The HTTP fetch helper is small and isolated. The DB writer touches only `us_symbol_universe.is_common_stock`. CLI defaults to dry-run.

### Step 2.1 — Pure classifier helper (TDD)

- [ ] **Write the failing test** in `tests/test_us_common_stock_classifier.py`:

```python
import pytest

from app.services.us_common_stock_classifier import classify_row


@pytest.mark.unit
def test_classify_row_common_aapl() -> None:
    row = {
        "Symbol": "AAPL",
        "ETF": "N",
        "Test Issue": "N",
        "Security Name": "Apple Inc. - Common Stock",
    }
    assert classify_row(row) is True


@pytest.mark.unit
def test_classify_row_etf_spy() -> None:
    row = {"Symbol": "SPY", "ETF": "Y", "Test Issue": "N", "Security Name": "..."}
    assert classify_row(row) is False


@pytest.mark.unit
def test_classify_row_test_issue() -> None:
    row = {"Symbol": "ZTEST", "ETF": "N", "Test Issue": "Y", "Security Name": "..."}
    assert classify_row(row) is False


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["BRK.PR.A", "ABCW", "AAA-R", "AAA-U", "FOO.WS"])
def test_classify_row_preferred_or_warrant(symbol: str) -> None:
    row = {"Symbol": symbol, "ETF": "N", "Test Issue": "N", "Security Name": "..."}
    assert classify_row(row) is False


@pytest.mark.unit
def test_classify_row_handles_caret_in_symbol() -> None:
    row = {"Symbol": "ZWZZT^", "ETF": "N", "Test Issue": "N", "Security Name": "..."}
    assert classify_row(row) is False
```

- [ ] **Run the test to verify it fails**

```bash
uv run pytest tests/test_us_common_stock_classifier.py -v
```

Expected: ImportError on `app.services.us_common_stock_classifier`.

- [ ] **Implement** `app/services/us_common_stock_classifier.py` (helper-only section):

```python
"""NASDAQ Trader-driven classifier for us_symbol_universe.is_common_stock.

Source files:
  https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.models.us_symbol_universe import USSymbolUniverse

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.nasdaqtrader.com/dynamic/SymDir"
_SOURCES: tuple[tuple[str, str], ...] = (
    ("nasdaqlisted.txt", "nasdaq"),
    ("otherlisted.txt", "other"),
)
_SUFFIX_NON_COMMON = re.compile(
    r"(\.PR\.[A-Z]|\.WS|\.P|-W$|-R$|-U$|\^)$"
)


def classify_row(row: dict[str, str]) -> bool:
    test_issue = (row.get("Test Issue") or "").strip().upper()
    if test_issue == "Y":
        return False
    etf = (row.get("ETF") or "").strip().upper()
    if etf == "Y":
        return False
    symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip().upper()
    if "^" in symbol:
        return False
    if _SUFFIX_NON_COMMON.search(symbol):
        return False
    return True
```

- [ ] **Run** — passing tests.

### Step 2.2 — Parse pipe-delimited NASDAQ Trader files

- [ ] **Write the failing test**:

```python
import textwrap

from app.services.us_common_stock_classifier import parse_nasdaq_trader_lines


def test_parse_nasdaq_trader_lines_skips_footer_and_header() -> None:
    body = textwrap.dedent(
        """\
        Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
        AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
        SPY|SPDR S&P 500 ETF Trust|P|N|N|100|Y|N
        File Creation Time: 2026-05-12T17:00:00|
        """
    )
    parsed = list(parse_nasdaq_trader_lines(body.splitlines()))
    symbols = {row["Symbol"] for row in parsed}
    assert symbols == {"AAPL", "SPY"}
```

- [ ] **Run** — fails (function not defined).

- [ ] **Implement** in `app/services/us_common_stock_classifier.py`:

```python
def parse_nasdaq_trader_lines(lines: Iterable[str]) -> Iterable[dict[str, str]]:
    """Yield dict rows from a NASDAQ Trader pipe-delimited file.

    Strips the trailing "File Creation Time" footer line and any blank lines.
    """
    reader = csv.DictReader(lines, delimiter="|")
    for row in reader:
        symbol = row.get("Symbol") or row.get("ACT Symbol")
        if not symbol:
            continue
        if symbol.startswith("File Creation Time"):
            continue
        yield row
```

- [ ] **Run** — passing tests.

### Step 2.3 — HTTP fetch + composed classifier

- [ ] **Write the failing test** with `respx` (already in the dev dependencies):

```python
import httpx
import pytest
import respx

from app.services.us_common_stock_classifier import build_classifications


@pytest.mark.asyncio
@respx.mock
async def test_build_classifications_merges_sources() -> None:
    respx.get(
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    ).mock(
        return_value=httpx.Response(
            200,
            text=(
                "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
                "AAPL|Apple Inc.|Q|N|N|100|N|N\n"
                "SPY|SPDR S&P 500 ETF Trust|P|N|N|100|Y|N\n"
            ),
        )
    )
    respx.get(
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
    ).mock(
        return_value=httpx.Response(
            200,
            text=(
                "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
                "BRK.B|Berkshire Hathaway|N|BRKB|N|100|N|BRK.B\n"
            ),
        )
    )
    result = await build_classifications()
    assert result["AAPL"] is True
    assert result["SPY"] is False
    assert result["BRK.B"] is True
```

- [ ] **Run** — fails.

- [ ] **Implement**:

```python
async def _download(source_name: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{_BASE_URL}/{source_name}")
        response.raise_for_status()
    return response.text.splitlines()


async def build_classifications() -> dict[str, bool]:
    """Return {symbol(db-form) -> is_common_stock_bool} for every classifiable row."""
    out: dict[str, bool] = {}
    for source_name, _ in _SOURCES:
        lines = await _download(source_name)
        for row in parse_nasdaq_trader_lines(lines):
            raw = (row.get("Symbol") or row.get("ACT Symbol") or "").strip().upper()
            db_symbol = to_db_symbol(raw)
            if not db_symbol:
                continue
            out[db_symbol] = classify_row(row)
    return out
```

- [ ] **Run** — passing tests.

### Step 2.4 — DB writer

- [ ] **Write the failing test** (using a transactional async session fixture):

```python
@pytest.mark.asyncio
async def test_sync_us_common_stock_flags_updates_only_matched_rows(
    async_session,  # provided by existing test fixtures
) -> None:
    from app.models.us_symbol_universe import USSymbolUniverse
    from app.services.us_common_stock_classifier import (
        apply_classifications,
        SyncResult,
    )

    async_session.add_all(
        [
            USSymbolUniverse(symbol="AAPL", exchange="NASD", name_kr="", name_en="Apple", is_active=True),
            USSymbolUniverse(symbol="SPY", exchange="NYSE", name_kr="", name_en="SPDR", is_active=True),
            USSymbolUniverse(symbol="ZZZZ", exchange="NASD", name_kr="", name_en="Foreign", is_active=True),
        ]
    )
    await async_session.flush()

    classifications = {"AAPL": True, "SPY": False}
    result: SyncResult = await apply_classifications(async_session, classifications)
    await async_session.commit()

    assert result.set_true == 1
    assert result.set_false == 1
    assert result.unchanged == 0
    assert result.unmatched == 0

    aapl = (await async_session.execute(
        sa.select(USSymbolUniverse).where(USSymbolUniverse.symbol == "AAPL")
    )).scalar_one()
    assert aapl.is_common_stock is True

    zzzz = (await async_session.execute(
        sa.select(USSymbolUniverse).where(USSymbolUniverse.symbol == "ZZZZ")
    )).scalar_one()
    assert zzzz.is_common_stock is None
```

- [ ] **Run** — fails.

- [ ] **Implement** in `app/services/us_common_stock_classifier.py`:

```python
@dataclass(frozen=True)
class SyncResult:
    set_true: int
    set_false: int
    unchanged: int
    unmatched: int


async def apply_classifications(
    session: AsyncSession, classifications: dict[str, bool]
) -> SyncResult:
    if not classifications:
        return SyncResult(0, 0, 0, 0)
    rows = list(
        (
            await session.execute(
                sa.select(USSymbolUniverse).where(
                    USSymbolUniverse.symbol.in_(classifications.keys())
                )
            )
        ).scalars()
    )
    set_true = set_false = unchanged = 0
    for row in rows:
        target = classifications[row.symbol]
        if row.is_common_stock == target:
            unchanged += 1
            continue
        row.is_common_stock = target
        if target:
            set_true += 1
        else:
            set_false += 1
    await session.flush()
    unmatched = len(classifications) - len(rows)
    return SyncResult(set_true, set_false, unchanged, unmatched)


async def sync_us_common_stock_flags(*, commit: bool = False) -> SyncResult:
    classifications = await build_classifications()
    async with AsyncSessionLocal() as session:
        result = await apply_classifications(session, classifications)
        if commit:
            await session.commit()
        else:
            await session.rollback()
    logger.info(
        "us_common_stock_classifier: set_true=%d set_false=%d unchanged=%d unmatched=%d commit=%s",
        result.set_true,
        result.set_false,
        result.unchanged,
        result.unmatched,
        commit,
    )
    return result
```

- [ ] **Run** — passing tests.

### Step 2.5 — Thin job wrapper

- [ ] **Write the failing test** that asserts the job returns a result dict shape:

```python
@pytest.mark.asyncio
async def test_run_us_common_stock_sync_job_dry_run(monkeypatch) -> None:
    from app.jobs.us_common_stock_classifier import run_us_common_stock_sync
    from app.services import us_common_stock_classifier as svc

    async def _fake_sync(*, commit: bool) -> svc.SyncResult:
        assert commit is False
        return svc.SyncResult(set_true=3, set_false=2, unchanged=1, unmatched=0)

    monkeypatch.setattr(svc, "sync_us_common_stock_flags", _fake_sync)
    payload = await run_us_common_stock_sync(commit=False)
    assert payload == {
        "status": "completed",
        "set_true": 3,
        "set_false": 2,
        "unchanged": 1,
        "unmatched": 0,
        "committed": False,
    }
```

- [ ] **Implement** `app/jobs/us_common_stock_classifier.py`:

```python
from __future__ import annotations

import logging

from app.services.us_common_stock_classifier import sync_us_common_stock_flags

logger = logging.getLogger(__name__)


async def run_us_common_stock_sync(*, commit: bool = False) -> dict[str, int | str | bool]:
    try:
        result = await sync_us_common_stock_flags(commit=commit)
        return {
            "status": "completed",
            "set_true": result.set_true,
            "set_false": result.set_false,
            "unchanged": result.unchanged,
            "unmatched": result.unmatched,
            "committed": commit,
        }
    except Exception as exc:
        logger.error("us_common_stock sync failed: %s", exc, exc_info=True)
        return {"status": "failed", "error": str(exc), "committed": False}
```

### Step 2.6 — CLI

- [ ] **Write the failing CLI test** in `tests/test_sync_us_common_stock_flags_cli.py`:

```python
import importlib

from scripts.sync_us_common_stock_flags import parse_args


def test_default_is_dry_run() -> None:
    args = parse_args([])
    assert args.commit is False


def test_commit_flag() -> None:
    args = parse_args(["--commit"])
    assert args.commit is True
```

- [ ] **Implement** `scripts/sync_us_common_stock_flags.py`:

```python
#!/usr/bin/env python3
"""Sync us_symbol_universe.is_common_stock from NASDAQ Trader (ROB-204).

Defaults to --dry-run (no DB writes). Pass --commit to persist.
"""
from __future__ import annotations

import argparse
import asyncio

from app.core.cli import setup_logging_and_sentry
from app.jobs.us_common_stock_classifier import run_us_common_stock_sync


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync us_symbol_universe.is_common_stock flag from NASDAQ Trader."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is --dry-run.",
    )
    return parser.parse_args(argv)


async def main() -> int:
    setup_logging_and_sentry(service_name="sync-us-common-stock-flags")
    args = parse_args()
    result = await run_us_common_stock_sync(commit=args.commit)
    print(
        f"\nstatus={result.get('status')} "
        f"set_true={result.get('set_true')} "
        f"set_false={result.get('set_false')} "
        f"unchanged={result.get('unchanged')} "
        f"unmatched={result.get('unmatched')} "
        f"committed={result.get('committed')}\n"
    )
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Run** — passing tests.

### Step 2.7 — Commit

```bash
git add app/services/us_common_stock_classifier.py \
        app/jobs/us_common_stock_classifier.py \
        scripts/sync_us_common_stock_flags.py \
        tests/test_us_common_stock_classifier.py \
        tests/test_sync_us_common_stock_flags_cli.py
git commit -m "feat(ROB-204): add NASDAQ Trader common-stock classifier + dry-run CLI"
```

---

## Task 3: Thread `common_stocks_only` through the snapshot job + resolvers

**Goal:** When `common_stocks_only=True` is passed to `SnapshotBuildRequest`, restrict the resolved symbol list to `us_symbol_universe.is_common_stock IS TRUE`. KR market raises an explicit `ValueError` (KR universe has no equivalent yet).

**Files:**
- Modify: `app/jobs/invest_screener_snapshots.py`
- Add tests in: `tests/test_invest_screener_snapshots_job.py` (create if absent — currently we have `test_build_invest_screener_snapshots_full_universe.py` covering `resolve_active_universe`).

**Acceptance:** Existing `common_stocks_only=False` behavior is byte-identical. With `common_stocks_only=True` against the US universe, only rows where `is_common_stock IS TRUE` are returned. KR raises. Empty result raises a clear `ValueError("…is_common_stock column not populated…")` when no rows have `is_common_stock IS NOT NULL`, so operators don't silently degrade.

### Step 3.1 — Write the failing job test

- [ ] **Write the failing test** in `tests/test_invest_screener_snapshots_job.py`:

```python
import pytest

from app.jobs import invest_screener_snapshots as snapshot_job
from app.models.us_symbol_universe import USSymbolUniverse


@pytest.mark.asyncio
async def test_resolve_active_universe_us_filters_common_stocks(async_session) -> None:
    async_session.add_all([
        USSymbolUniverse(symbol="AAPL", exchange="NASD", name_kr="", name_en="Apple", is_active=True, is_common_stock=True),
        USSymbolUniverse(symbol="SPY", exchange="NYSE", name_kr="", name_en="SPDR", is_active=True, is_common_stock=False),
        USSymbolUniverse(symbol="BRK.B", exchange="NYSE", name_kr="", name_en="Berkshire", is_active=True, is_common_stock=True),
        USSymbolUniverse(symbol="ZZZZ", exchange="NASD", name_kr="", name_en="Foreign", is_active=True, is_common_stock=None),
    ])
    await async_session.commit()

    symbols = await snapshot_job.resolve_active_universe("us", common_stocks_only=True)
    assert sorted(symbols) == ["AAPL", "BRK.B"]


@pytest.mark.asyncio
async def test_resolve_active_universe_kr_rejects_common_stocks_only() -> None:
    with pytest.raises(ValueError, match="common_stocks_only is not supported for market=kr"):
        await snapshot_job.resolve_active_universe("kr", common_stocks_only=True)


@pytest.mark.asyncio
async def test_resolve_active_universe_us_raises_when_column_unpopulated(async_session) -> None:
    async_session.add_all([
        USSymbolUniverse(symbol="ZZZZ", exchange="NASD", name_kr="", name_en="X", is_active=True, is_common_stock=None),
    ])
    await async_session.commit()
    with pytest.raises(ValueError, match="is_common_stock column is not populated"):
        await snapshot_job.resolve_active_universe("us", common_stocks_only=True)
```

- [ ] **Run** — fails (`TypeError` on the unknown kwarg).

### Step 3.2 — Extend the request dataclass

- [ ] **Edit** `app/jobs/invest_screener_snapshots.py`:

```python
@dataclass(frozen=True)
class SnapshotBuildRequest:
    market: str
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 200
    concurrency: int = 4
    commit: bool = False
    today: dt.date | None = None
    common_stocks_only: bool = False
```

### Step 3.3 — Extend the universe resolvers

- [ ] **Edit** `app/jobs/invest_screener_snapshots.py`:

```python
async def resolve_active_universe(
    market: str, *, common_stocks_only: bool = False
) -> list[str]:
    _validate_market(market)
    if common_stocks_only and market != "us":
        raise ValueError(
            "common_stocks_only is not supported for market=kr (no classifier)"
        )
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            if common_stocks_only:
                classified_stmt = sa.select(sa.func.count()).select_from(
                    USSymbolUniverse
                ).where(USSymbolUniverse.is_common_stock.is_not(None))
                classified = int((await session.execute(classified_stmt)).scalar() or 0)
                if classified == 0:
                    raise ValueError(
                        "us_symbol_universe.is_common_stock column is not populated. "
                        "Run `uv run python -m scripts.sync_us_common_stock_flags --commit` first."
                    )
                stmt = (
                    sa.select(USSymbolUniverse.symbol)
                    .where(USSymbolUniverse.is_active.is_(True))
                    .where(USSymbolUniverse.is_common_stock.is_(True))
                    .order_by(USSymbolUniverse.symbol)
                )
            else:
                stmt = (
                    sa.select(USSymbolUniverse.symbol)
                    .where(USSymbolUniverse.is_active.is_(True))
                    .order_by(USSymbolUniverse.symbol)
                )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]
```

(Apply the same `common_stocks_only` filter to `resolve_symbols` when `override` is empty so that `--common-stocks-only --limit 50` returns 50 common stocks. When an explicit `--symbol` override list is provided, the override wins — the classifier is not enforced; the operator stated the symbols explicitly.)

### Step 3.4 — Thread the flag through `run_snapshot_build`

- [ ] **Edit** the body of `run_snapshot_build`:

```python
    if request.all_symbols:
        symbols = await resolve_active_universe(
            request.market, common_stocks_only=request.common_stocks_only
        )
    else:
        symbols = await resolve_symbols(
            request.market,
            list(request.symbols),
            request.limit or 20,
            common_stocks_only=request.common_stocks_only,
        )
```

…and update `resolve_symbols` to accept and apply `common_stocks_only` when `override` is empty (skip the filter when `override` is non-empty, since the operator provided explicit symbols).

### Step 3.5 — Run job tests

```bash
uv run pytest tests/test_invest_screener_snapshots_job.py -v
uv run pytest tests/test_build_invest_screener_snapshots_full_universe.py -v
```

Expected: PASS.

### Step 3.6 — Commit

```bash
git add app/jobs/invest_screener_snapshots.py tests/test_invest_screener_snapshots_job.py
git commit -m "feat(ROB-204): thread common_stocks_only filter through snapshot job"
```

---

## Task 4: CLI + TaskIQ flag plumbing

**Goal:** Surface `--common-stocks-only` on the operator CLI and pass it through the TaskIQ wrapper.

**Files:**
- Modify: `scripts/build_invest_screener_snapshots.py`
- Modify: `app/tasks/invest_screener_snapshot_tasks.py`
- Modify: `tests/test_build_invest_screener_snapshots_cli.py`
- Modify: `tests/test_invest_screener_snapshot_tasks.py`

**Acceptance:** CLI `--common-stocks-only` flag is parsed and threaded. Combined with `--market kr` it raises (parser error) before any DB connection. TaskIQ task forwards the flag.

### Step 4.1 — CLI flag

- [ ] **Write the failing CLI test**:

```python
def test_parse_common_stocks_only_us() -> None:
    args = parse_args(["--market", "us", "--all", "--common-stocks-only"])
    assert args.common_stocks_only is True


def test_parse_common_stocks_only_kr_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--market", "kr", "--all", "--common-stocks-only"])
```

- [ ] **Edit** `scripts/build_invest_screener_snapshots.py`:

```python
parser.add_argument(
    "--common-stocks-only",
    action="store_true",
    help=(
        "Restrict the universe to us_symbol_universe.is_common_stock IS TRUE. "
        "Only valid with --market us."
    ),
)
# ...
if args.common_stocks_only and args.market != "us":
    parser.error("--common-stocks-only is only valid with --market us")
```

…then thread `args.common_stocks_only` into `SnapshotBuildRequest`.

### Step 4.2 — TaskIQ wrapper flag

- [ ] **Write the failing test** in `tests/test_invest_screener_snapshot_tasks.py`:

```python
@pytest.mark.asyncio
async def test_task_forwards_common_stocks_only(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_run(request):
        captured["request"] = request
        from app.jobs.invest_screener_snapshots import SnapshotBuildResult
        return SnapshotBuildResult(
            market=request.market,
            symbols_resolved=0,
            snapshots_built=0,
            skipped=0,
            committed=False,
            batches=0,
            started_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
        )

    monkeypatch.setattr(
        "app.tasks.invest_screener_snapshot_tasks.run_snapshot_build",
        _fake_run,
    )
    from app.tasks.invest_screener_snapshot_tasks import (
        build_invest_screener_snapshots,
    )
    payload = await build_invest_screener_snapshots(
        market="us", all_symbols=True, common_stocks_only=True
    )
    assert captured["request"].common_stocks_only is True
    assert payload["market"] == "us"
```

- [ ] **Edit** `app/tasks/invest_screener_snapshot_tasks.py` to accept `common_stocks_only: bool = False` and forward to `SnapshotBuildRequest`.

### Step 4.3 — Commit

```bash
git add scripts/build_invest_screener_snapshots.py \
        app/tasks/invest_screener_snapshot_tasks.py \
        tests/test_build_invest_screener_snapshots_cli.py \
        tests/test_invest_screener_snapshot_tasks.py
git commit -m "feat(ROB-204): wire --common-stocks-only through CLI + TaskIQ task"
```

---

## Task 5: View-model user-facing warning for missing US snapshots

**Goal:** When `requested_market == "us"` and `_aggregated_data_state in {"missing", "stale"}`, prepend a localized warning string to the response so the React UI can surface it without re-deriving state.

**Files:**
- Modify: `app/services/invest_view_model/screener_service.py`
- Create: `tests/test_screener_us_missing_warning.py` (or extend `tests/test_invest_view_model_screener_service.py`).

**Acceptance:** For `market=="us"`, `dataState=="missing"` → response contains exactly one warning string matching `"미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."`. For `market=="us"`, `dataState=="stale"` → contains `"미국 스크리너 데이터가 오래되었습니다 — 갱신 대기 중"`. For `market=="kr"` or `dataState=="fresh"`, no new warning added. Existing upstream warnings remain unchanged.

### Step 5.1 — Failing test

- [ ] **Write the failing test**:

```python
import pytest

from app.services.invest_view_model.screener_service import build_screener_results


class _Resolver:
    def relation(self, market, symbol):
        return "none"


class _EmptyScreener:
    async def list_screening(self, **kwargs):
        return {"results": [], "warnings": [], "timestamp": "2026-05-12T00:00:00+00:00", "cache_hit": False}


@pytest.mark.asyncio
async def test_us_consecutive_gainers_missing_emits_user_facing_warning() -> None:
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=_EmptyScreener(),
        resolver=_Resolver(),
        market="us",
        session=None,
    )
    # dataState defaults to "missing" with no session/rows
    assert response.freshness.dataState == "missing"
    assert any(
        "미국 스크리너 데이터 준비중" in w for w in response.warnings
    ), response.warnings


@pytest.mark.asyncio
async def test_kr_consecutive_gainers_missing_does_not_emit_us_warning() -> None:
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=_EmptyScreener(),
        resolver=_Resolver(),
        market="kr",
        session=None,
    )
    assert not any(
        "미국 스크리너" in w for w in response.warnings
    ), response.warnings
```

- [ ] **Run** — fails.

### Step 5.2 — Implement the warning

- [ ] **Edit** `app/services/invest_view_model/screener_service.py`. After the `_aggregated_data_state` is computed and before the `freshness = _build_freshness(...)` call, append:

```python
if (
    requested_market == "us"
    and preset_id == "consecutive_gainers"
    and _aggregated_data_state in {"missing", "stale"}
):
    user_warning = (
        "미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."
        if _aggregated_data_state == "missing"
        else "미국 스크리너 데이터가 오래되었습니다 — 갱신 대기 중"
    )
    if user_warning not in upstream_warnings:
        upstream_warnings.insert(0, user_warning)
```

- [ ] **Run** — passing tests.

### Step 5.3 — Commit

```bash
git add app/services/invest_view_model/screener_service.py \
        tests/test_screener_us_missing_warning.py
git commit -m "feat(ROB-204): emit user-facing warning when US screener dataState is missing/stale"
```

---

## Task 6: Frontend `dataState` plumbing

**Goal:** Add `dataState` to the frontend `ScreenerFreshness` type and render a chip in `ScreenerFreshnessLine` for the non-fresh states.

**Files:**
- Modify: `frontend/invest/src/types/screener.ts`
- Modify: `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`
- Modify: `frontend/invest/src/desktop/screener/screener.css`
- Modify: `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`

**Acceptance:** Type matches Pydantic schema. The chip renders only for `dataState ∈ {"missing", "stale", "fallback", "partial"}`. Existing tests still pass. New tests cover each non-fresh state. Visually, `dataState="missing"` displays a muted-warning chip in Korean.

### Step 6.1 — Type update

- [ ] **Edit** `frontend/invest/src/types/screener.ts`:

```ts
export type ScreenerDataState = "fresh" | "partial" | "stale" | "missing" | "fallback";

export interface ScreenerFreshness {
  fetchedAt: string;
  asOfLabel: string;
  relativeLabel: string;
  cacheHit: boolean;
  source: ScreenerFreshnessSource;
  dataState: ScreenerDataState;
}
```

### Step 6.2 — Failing test for chip rendering

- [ ] **Edit** `frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx`:

```ts
test("renders missing-data chip when dataState is missing", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-05-12T00:00:00+00:00",
        asOfLabel: "2026.05.12 09:00 기준",
        relativeLabel: "방금 갱신",
        cacheHit: false,
        source: "live",
        dataState: "missing",
      }}
    />,
  );
  expect(screen.getByTestId("screener-freshness-state")).toHaveTextContent(
    "준비중",
  );
});

test("renders stale chip when dataState is stale", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-05-12T00:00:00+00:00",
        asOfLabel: "2026.05.10 16:00 기준",
        relativeLabel: "이틀 전 갱신",
        cacheHit: true,
        source: "cached",
        dataState: "stale",
      }}
    />,
  );
  expect(screen.getByTestId("screener-freshness-state")).toHaveTextContent(
    "갱신 대기",
  );
});

test("renders no chip when dataState is fresh", () => {
  render(
    <ScreenerFreshnessLine
      freshness={{
        fetchedAt: "2026-05-12T00:00:00+00:00",
        asOfLabel: "2026.05.12 09:00 기준",
        relativeLabel: "방금 갱신",
        cacheHit: false,
        source: "live",
        dataState: "fresh",
      }}
    />,
  );
  expect(screen.queryByTestId("screener-freshness-state")).toBeNull();
});
```

### Step 6.3 — Implement the chip

- [ ] **Edit** `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx`:

```tsx
import type { ScreenerDataState, ScreenerFreshness } from "../../types/screener";

const STATE_LABELS: Partial<Record<ScreenerDataState, string>> = {
  missing: "준비중",
  stale: "갱신 대기",
  fallback: "일부 보강",
  partial: "기간 짧음",
};

export function ScreenerFreshnessLine({
  freshness,
}: {
  freshness: ScreenerFreshness;
}) {
  const text =
    freshness.source === "previous_session"
      ? `${freshness.relativeLabel} · ${freshness.asOfLabel.replace("기준", "종가")}`
      : `${freshness.asOfLabel} · ${freshness.relativeLabel}`;
  const stateLabel = STATE_LABELS[freshness.dataState];
  return (
    <div
      className="screener-freshness"
      data-testid="screener-freshness"
      aria-live="polite"
    >
      <span>{text}</span>
      {stateLabel && (
        <span
          className={`screener-freshness-state screener-freshness-state--${freshness.dataState}`}
          data-testid="screener-freshness-state"
          role="status"
        >
          {stateLabel}
        </span>
      )}
    </div>
  );
}
```

- [ ] **Edit** `frontend/invest/src/desktop/screener/screener.css` to add the chip styles:

```css
.screener-freshness {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.screener-freshness-state {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  line-height: 18px;
  background: var(--surface-3, #f0f0f0);
  color: var(--fg-2, #555);
}
.screener-freshness-state--missing { background: var(--warn-bg, #fff5e6); color: var(--warn-fg, #b35900); }
.screener-freshness-state--stale   { background: var(--surface-3, #f0f0f0); color: var(--fg-2, #555); }
.screener-freshness-state--fallback{ background: var(--surface-3, #f0f0f0); color: var(--fg-2, #555); }
.screener-freshness-state--partial { background: var(--surface-3, #f0f0f0); color: var(--fg-2, #555); }
```

- [ ] **Run** frontend tests:

```bash
cd frontend/invest && pnpm test --run ScreenerFreshnessLine
```

Expected: PASS.

### Step 6.4 — Commit

```bash
git add frontend/invest/src/types/screener.ts \
        frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx \
        frontend/invest/src/desktop/screener/screener.css \
        frontend/invest/src/__tests__/ScreenerFreshnessLine.test.tsx
git commit -m "feat(ROB-204): render dataState chip on screener freshness line"
```

---

## Task 7: Prefect flow scaffold (deployment DEFERRED)

**Goal:** Land an importable Prefect flow that wraps `run_snapshot_build(market="us", all_symbols=True, common_stocks_only=True, commit=<INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED>)`. The flow is registered as `@flow` so it's discoverable, but no Prefect deployment manifest is added. Recurring activation is a separate operator action.

**Files:**
- Modify: `app/core/config.py` — add `invest_screener_snapshots_commit_enabled: bool = False`.
- Create: `app/flows/invest_screener_snapshots_us_flow.py`
- Create: `tests/test_invest_screener_snapshots_us_flow.py`

**Acceptance:** Import works without Prefect server. Calling the underlying coroutine with `commit_enabled=False` results in `commit=False` passed to `run_snapshot_build`. Calling with `commit_enabled=True` passes `commit=True`. The flow body must not print env values, secrets, or full payloads at INFO level.

### Step 7.1 — Settings flag

- [ ] **Edit** `app/core/config.py`. Add the setting next to the existing snapshot/feature flags:

```python
invest_screener_snapshots_commit_enabled: bool = Field(
    default=False,
    description=(
        "When True, the post-US-close Prefect flow persists snapshot rows. "
        "Default False keeps the flow in dry-run for safe smoke runs."
    ),
)
```

(Use whatever pattern the existing Settings class uses for booleans. If `pydantic_settings`, add to the `Settings` class and rely on `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` env var.)

### Step 7.2 — Failing flow test

- [ ] **Write the failing test** in `tests/test_invest_screener_snapshots_us_flow.py`:

```python
import datetime as dt

import pytest


@pytest.mark.asyncio
async def test_us_flow_dry_run_does_not_persist(monkeypatch) -> None:
    captured = {}

    async def _fake_run(request):
        captured["request"] = request
        from app.jobs.invest_screener_snapshots import SnapshotBuildResult
        return SnapshotBuildResult(
            market="us",
            symbols_resolved=10,
            snapshots_built=10,
            skipped=0,
            committed=False,
            batches=1,
            started_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
        )

    monkeypatch.setattr(
        "app.flows.invest_screener_snapshots_us_flow.run_snapshot_build",
        _fake_run,
    )

    from app.flows.invest_screener_snapshots_us_flow import (
        run_us_screener_snapshot_refresh,
    )
    payload = await run_us_screener_snapshot_refresh(commit_enabled=False)
    assert captured["request"].commit is False
    assert captured["request"].all_symbols is True
    assert captured["request"].common_stocks_only is True
    assert payload["committed"] is False


@pytest.mark.asyncio
async def test_us_flow_commit_enabled_persists(monkeypatch) -> None:
    captured = {}

    async def _fake_run(request):
        captured["request"] = request
        from app.jobs.invest_screener_snapshots import SnapshotBuildResult
        return SnapshotBuildResult(
            market="us",
            symbols_resolved=10,
            snapshots_built=10,
            skipped=0,
            committed=True,
            batches=1,
            started_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC),
        )

    monkeypatch.setattr(
        "app.flows.invest_screener_snapshots_us_flow.run_snapshot_build",
        _fake_run,
    )

    from app.flows.invest_screener_snapshots_us_flow import (
        run_us_screener_snapshot_refresh,
    )
    payload = await run_us_screener_snapshot_refresh(commit_enabled=True)
    assert captured["request"].commit is True
    assert payload["committed"] is True
```

- [ ] **Run** — fails (module missing).

### Step 7.3 — Implement the flow

- [ ] **Create** `app/flows/invest_screener_snapshots_us_flow.py`:

```python
"""Post-US-close screener snapshot refresh (ROB-204).

Activation is gated on INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED.
Importing this module is always safe; no Prefect deployment is registered here.
"""
from __future__ import annotations

import logging

from prefect import flow, task

from app.core.config import settings
from app.jobs.invest_screener_snapshots import (
    SnapshotBuildRequest,
    run_snapshot_build,
)

logger = logging.getLogger(__name__)


@task
async def run_us_screener_snapshot_refresh(
    *, commit_enabled: bool | None = None
) -> dict[str, object]:
    if commit_enabled is None:
        commit_enabled = bool(
            getattr(settings, "invest_screener_snapshots_commit_enabled", False)
        )
    request = SnapshotBuildRequest(
        market="us",
        all_symbols=True,
        common_stocks_only=True,
        batch_size=200,
        concurrency=4,
        commit=commit_enabled,
    )
    result = await run_snapshot_build(request)
    payload = {
        "market": result.market,
        "symbolsResolved": result.symbols_resolved,
        "snapshotsBuilt": result.snapshots_built,
        "skipped": result.skipped,
        "committed": result.committed,
        "batches": result.batches,
        "snapshotDateDistribution": result.snapshot_date_distribution,
        "warnings": list(result.warnings),
    }
    logger.info(
        "invest_screener_snapshots_us_flow: built=%d resolved=%d committed=%s batches=%d",
        result.snapshots_built,
        result.symbols_resolved,
        result.committed,
        result.batches,
    )
    return payload


@flow(name="invest_screener_snapshots_us")
async def invest_screener_snapshots_us_flow() -> dict[str, object]:
    return await run_us_screener_snapshot_refresh()
```

- [ ] **Run** — passing tests.

### Step 7.4 — Commit

```bash
git add app/core/config.py \
        app/flows/invest_screener_snapshots_us_flow.py \
        tests/test_invest_screener_snapshots_us_flow.py
git commit -m "feat(ROB-204): add Prefect flow scaffold for post-US-close snapshot refresh (deployment deferred)"
```

---

## Task 8: Runbook + CLAUDE.md updates

**Goal:** Capture the activation evidence sequence, the Prefect deployment-deferred state, and the new safety boundaries.

**Files:**
- Modify: `docs/runbooks/invest-screener-snapshots.md`
- Modify: `CLAUDE.md`

**Acceptance:** Runbook §2 mentions the new `--common-stocks-only` flag. A new §7 captures the full US activation sequence (Phases 0–5 from this plan). A new §8 captures the Prefect deployment-deferred status with the intended cron expression and the activation gate. CLAUDE.md grows one short section under the existing ROB-170 paragraph.

### Step 8.1 — Edit the runbook

- [ ] **Edit** `docs/runbooks/invest-screener-snapshots.md`. After the existing §6 "Safety Boundary", append:

```markdown
---

## 7. US Activation Procedure (ROB-204)

This section is the operator playbook for the first US production write.
All steps below assume the operator is reading from an approved Linear thread on ROB-204.

### Phase 0 — Pre-flight (read-only)

… (paste Phase 0 from the plan) …

### Phase 1 — Populate `is_common_stock`

```
uv run python -m scripts.sync_us_common_stock_flags             # dry-run
uv run python -m scripts.sync_us_common_stock_flags --commit    # persist
```

### Phase 2 — Bounded US dry-run

```
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only
```

### Phase 3 — Reviewer approval round (Linear)

(paste the approval template from the plan)

### Phase 4 — Bounded US commit

```
uv run python -m scripts.build_invest_screener_snapshots \
    --market us --all --common-stocks-only --commit
```

### Phase 5 — Spot-check `/invest/screener?market=us`

(paste from the plan)

---

## 8. Prefect Deployment (DEFERRED)

A Prefect flow `invest_screener_snapshots_us` is registered in
`app/flows/invest_screener_snapshots_us_flow.py`. The deployment manifest is
intentionally not added in the ROB-204 PR.

When operator approval is received:

```
export INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED=true
prefect deployment apply path/to/invest-screener-snapshots-us-deployment.yaml
prefect deployment run 'invest_screener_snapshots_us/post-us-close'
```

Intended schedule: `30 21 * * 1-5` UTC (≈17:30 ET, ~30 min after the regular
US session close). Use the same TZ semantics as the existing
`forexfactory_calendar_rolling_window_flow`.
```

### Step 8.2 — Edit CLAUDE.md

- [ ] **Edit** `CLAUDE.md`. Find the existing ROB-170 (`### invest_screener_snapshots Schema`) or analogous section and append:

```markdown
### Invest Screener US Activation (ROB-204)

US `consecutive_gainers` screener serves snapshot-backed rows via
`invest_screener_snapshots`. The first US production write was gated on:

- additive `us_symbol_universe.is_common_stock` column (alembic head
  `<auto>_add_us_symbol_universe_is_common_stock`)
- `scripts/sync_us_common_stock_flags.py` (NASDAQ Trader-driven classifier)
- `scripts/build_invest_screener_snapshots.py --market us --all
  --common-stocks-only --commit` after dry-run evidence + reviewer approval
- a user-facing warning ("미국 스크리너 데이터 준비중") emitted by
  `app/services/invest_view_model/screener_service.py` when
  `dataState ∈ {"missing", "stale"}` for `market=="us"`
- `frontend/invest/src/desktop/screener/ScreenerFreshnessLine.tsx` renders a
  freshness chip for the non-fresh `dataState` values

Recurring refresh is a Prefect flow
(`app/flows/invest_screener_snapshots_us_flow.py`); the deployment is **not**
registered until a separate operator approval. The flow body honors
`INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` (default `False` ⇒ dry-run).
```

### Step 8.3 — Commit

```bash
git add docs/runbooks/invest-screener-snapshots.md CLAUDE.md
git commit -m "docs(ROB-204): runbook + CLAUDE.md updates for US screener activation"
```

---

## Task 9: PR open + activation handoff

**Goal:** Open the PR with the full activation packet so the reviewer can run Phases 0–5 themselves after merge.

**Files:** none changed (administrative task).

**Acceptance:** PR body includes a checklist mirroring the approval gates above, links to the runbook, calls out that no production write or Prefect deployment is registered in this PR, and includes a placeholder for the dry-run evidence the reviewer will run.

### Step 9.1 — Push and open the PR

```bash
git push -u origin kanban/ROB-204-us-screener-activation

gh pr create --title "feat(ROB-204): activate US screener snapshot commit path (common-stock filter + UI/API warning + Prefect scaffold)" \
  --body "$(cat <<'EOF'
## Summary

- Add `us_symbol_universe.is_common_stock` (additive nullable column + partial index) and a NASDAQ Trader-driven classifier (`scripts/sync_us_common_stock_flags.py`).
- Thread `--common-stocks-only` through the snapshot job, CLI, and TaskIQ wrapper so the first US `--commit` run is scoped to ~3–4K rows.
- Emit a user-facing warning (`"미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."`) from the view-model when US `dataState ∈ {"missing", "stale"}`.
- Surface `dataState` on the frontend `ScreenerFreshness` type and render a chip in `ScreenerFreshnessLine` for non-fresh states.
- Add a Prefect flow `invest_screener_snapshots_us_flow` gated by `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` (default `False`); the deployment is **not** registered in this PR.

## What this PR does NOT do

- No production DB write triggered from CI/dev.
- No Prefect deployment registered.
- No TaskIQ cron schedule added (operator-controlled task only).
- No broker / order / watch / order-intent / paper-trading code path changed.

## Activation runbook (operator-only, post-merge)

See `docs/runbooks/invest-screener-snapshots.md` §7 (US activation procedure)
and §8 (Prefect deployment, deferred).

## Test plan

- [ ] `uv run pytest tests/test_us_common_stock_classifier.py -v`
- [ ] `uv run pytest tests/test_sync_us_common_stock_flags_cli.py -v`
- [ ] `uv run pytest tests/test_us_symbol_universe_model.py -v`
- [ ] `uv run pytest tests/test_invest_screener_snapshots_job.py -v`
- [ ] `uv run pytest tests/test_build_invest_screener_snapshots_cli.py -v`
- [ ] `uv run pytest tests/test_invest_screener_snapshot_tasks.py -v`
- [ ] `uv run pytest tests/test_screener_us_missing_warning.py -v`
- [ ] `uv run pytest tests/test_invest_screener_snapshots_us_flow.py -v`
- [ ] `cd frontend/invest && pnpm test --run ScreenerFreshnessLine`
- [ ] `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` (local disposable DB)

## Approval gates (restated)

- [ ] No `--commit` invoked from CI/local for US.
- [ ] Operator-only Phases 0–5 of the runbook to be executed after merge.
- [ ] Prefect deployment registration deferred to a separate ticket.
- [ ] `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` defaults to `False`.
EOF
)"
```

### Step 9.2 — Record the PR URL in the Kanban handoff

The K5 reviewer (Opus) reads the PR URL and the activation gates; the operator (separate session, post-approval) runs the Phase 0–5 sequence.

---

## Self-Review (planner)

After writing this plan, I checked it against the K5 task body:

1. **Spec coverage:**
   - "Inspect current code/PR state" — done in the §Worktree note and Pre-conditions reading; PR #793 contents were read via `git show 6024a264`.
   - "Produce exact next graph steps for code fixes vs operational activation" — Tasks 1–8 are the code fixes; the §Operational activation procedure documents the operator-only commands.
   - "Identify scripts/commands for US dry-run evidence, bounded commit proposal, common-stock/universe filter, UI/API missing-snapshot warning, and Prefect post-US-close schedule" — Phases 0–5 (dry-run, commit), Task 2 (common-stock filter), Task 5 (API warning), Task 6 (UI warning), Task 7 + Phase 6 (Prefect).
   - "Do not run any production write/commit/scheduler activation" — explicit in every approval-gate section.

2. **Placeholder scan:** All TBDs replaced with concrete code/commands. The only literal `<auto>` placeholders are alembic revision IDs (filled at generation time) and the down_revision (whose value depends on the rebase target — explicitly called out in Task 1.3).

3. **Type / identifier consistency:**
   - `SnapshotBuildRequest.common_stocks_only` — used in Tasks 3, 4, 7.
   - `is_common_stock` — column name consistent across Tasks 1, 2, 3.
   - `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED` env var consistent across Task 7, runbook §8, CLAUDE.md.
   - `dataState` chip values: `"fresh" | "partial" | "stale" | "missing" | "fallback"` — same in Pydantic schema and frontend type.

4. **Spec gaps:** None identified. The plan deliberately does not register a Prefect deployment or wire a TaskIQ cron; the K5 task body says "Do not run any production write/commit/scheduler activation."

---

## Glossary

- **Snapshot foundation** = the ROB-170 set: `invest_screener_snapshots` table, builder, freshness, coverage service, repository, view-model snapshot-first read path.
- **PR #793 seam** = the ROB-204 dry-run boundary: `SnapshotBuildRequest`/`SnapshotBuildResult` dataclasses, `run_snapshot_build`, `scripts/build_invest_screener_snapshots.py` with `--commit` default-off, `app/tasks/invest_screener_snapshot_tasks.py` TaskIQ wrapper.
- **Common-stock filter** = `us_symbol_universe.is_common_stock IS TRUE` filter, populated by NASDAQ Trader files.
- **Approval gate** = an operator/reviewer step the implementer must not bypass. All gates restated in §Approval Gates.
- **Hermes-cron bridge** = ad-hoc cron entries registered through `app/tasks/*.py` `@broker.task(schedule=...)` decorators. Explicitly avoided for ROB-204; Prefect is the long-term home for recurring refresh.
