# MGH-7 — `invest_screener_snapshots` Snapshot-Backed `/invest/screener` First Slice

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a per-symbol, per-day `invest_screener_snapshots` table that precomputes the price-derived metrics (`consecutive_up_days`, `week_change_rate`, `latest_close`, `change_amount`, `change_rate`) consumed by `/invest/screener`'s `consecutive_gainers` preset. Wire `/invest/screener` enrichment to read from this snapshot first and fall back transparently to the existing on-demand OHLCV path (ROB-168) when the snapshot is `missing`/`stale`. Surface a per-response `dataState` so the UI/operator can see whether the table is fresh, stale, or being bypassed.

**Architecture:**
- **New write path:** an operator-run CLI (`scripts/build_invest_screener_snapshots.py`) iterates a small symbol slice (KR/US universe ∩ active), reuses `_fetch_ohlcv_for_indicators(symbol, market_type, count=10)` to pull the last 10 daily closes once per symbol per day, computes derived metrics, and upserts one row per `(market, symbol, snapshot_date)` via the new repository.
- **New read path:** `app.mcp_server.tooling.screening.enrichment._enrich_consecutive_up_days` (added by ROB-168) gains a snapshot-first short-circuit: bulk-load fresh snapshot rows for all candidate symbols, populate `consecutive_up_days` / `week_change_rate` / `change_*` from them, and only fall through to the existing per-symbol OHLCV fetch for rows whose snapshot is missing or stale. The view-model layer surfaces aggregate `dataState` on `ScreenerFreshness`.
- **No scheduler activation in this PR.** The migration creates the table; the CLI fills it on operator demand. A separate ticket will land the recurring fill once we have one or two days of operator-run smoke evidence.
- **No broker/order/watch mutations.** Read/model/UI/data-layer only.

**Tech Stack:** Python 3.13 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, pytest, ruff/ty); React layer untouched in this slice (frontend already consumes `ScreenerFreshness` from ROB-168 and will display the new `dataState` value as a badge in a follow-up).

**Linear:** https://linear.app/mgh3326/issue/MGH-7

**Branch:** `feature/ROB-170-invest-screener-snapshots` (the worktree directory is named `MGH-7-invest-screener-snapshots` for human-readable mapping to the Linear ticket; the actual git branch under it is `feature/ROB-170-invest-screener-snapshots`. Verify with `git -C /Users/mgh3326/worktrees/auto_trader/MGH-7-invest-screener-snapshots branch --show-current` before pushing.)

**Worktree:** `/Users/mgh3326/worktrees/auto_trader/MGH-7-invest-screener-snapshots` — implementer MUST work here. **Hard rule:** never edit `/Users/mgh3326/services/auto_trader/current` or `~/auto_trader` (root) directly.

**Depends on (must already be merged before this lands):** ROB-168 — adds `min_consecutive_up_days`, `_enrich_consecutive_up_days(rows, market)`, and `ScreenerFreshness` block. If ROB-168 has not merged when implementer starts, rebase against `origin/main` first and confirm both helper symbols exist via `grep` before doing any work; otherwise stop and escalate.

**Out of scope (explicitly):**
- Scheduler / Prefect / TaskIQ recurring trigger for the builder (separate ticket).
- Other presets (`cheap_value`, `steady_dividend`, `oversold_recovery`, `high_volume_momentum`, `growth_expectation`) — they continue to use upstream tvscreener / fundamental enrichment as today; only `consecutive_gainers` reads the snapshot.
- Crypto market — snapshots are KR + US equities only; crypto enrichment continues to flow through Upbit live.
- Frontend `dataState` badge styling — the data-state field is added to the API contract here; the UI badge is a follow-up.

---

## Decision Log (locked before tasks begin)

### D1. Snapshot table grain: `(market, symbol, snapshot_date)`
- One row per (market, symbol, trading-date the snapshot covers).
- `snapshot_date` is the calendar date *of the latest close used to compute the row*, not the wall-clock date the row was written.
  - Rationale: lets the read path filter by "snapshot covers today's KR session" without joining `computed_at`. `created_at` / `updated_at` track wall-clock for diagnostics.
- Composite UniqueConstraint mirrors `paper.paper_daily_snapshots` style.

### D2. Store derived metrics + a 10-element closes window (JSONB)
- Storing the closes window costs ~80 bytes/row but lets a future audit recompute `consecutive_up_days` without re-fetching OHLCV. Cheap insurance.
- We do **not** normalize closes into per-day rows — that's a separate `kr_candles_daily` discussion (out of scope; daily KIS data flows through `kis_ohlcv_cache` and Yahoo, not a hypertable today; the existing `public.kr_candles_1m` is intraday-only).

### D3. KR Korean-name precedence
- The snapshot table does **not** store `name`. Single source of truth = `kr_symbol_universe.name` (Korean-only, `String(100)`).
- The screener view-model already pulls Korean name from upstream tvscreener / fallback paths; this PR adds an explicit assertion test (`tests/test_invest_screener_snapshots_name_precedence.py`) that for any KR row enriched from a snapshot, the rendered `name` equals `kr_symbol_universe.name` for that symbol whenever the universe row exists, and falls back to `description` → `name` → `symbol` only when the universe row is missing.
- US precedence (`us_symbol_universe.name_kr` then `name_en`) is unchanged; we explicitly cover it in the same test file with a `name_kr=None` fallback case.

### D4. `dataState` enum
Mirrors ROB-167 calendar conventions (`app/schemas/calendar_freshness.py`) but scoped to screener snapshots:

| Value         | Trigger                                                                           |
|---------------|-----------------------------------------------------------------------------------|
| `fresh`       | snapshot row exists for current trading-date and `len(closes_window) >= 5`        |
| `partial`     | row exists but `2 <= len(closes_window) < 5` (streak computable, week_change not) |
| `stale`       | row exists but `snapshot_date` is older than the most recent KR/US trading session by ≥ 1 trading day, OR `computed_at` older than 36h |
| `missing`     | no row found for any candidate symbol of the response                             |
| `fallback`    | snapshot was bypassed and on-demand OHLCV path was used for ≥ 1 row               |

`ScreenerFreshness.dataState` reports the *worst* state across the response's rows (e.g. mixed fresh+stale → `stale`; any missing+otherwise-fresh → `fallback` because the read path filled the gap on demand).

The 36h staleness window is shared with ROB-167 (`STALE_AFTER_HOURS = 36` in `app/services/market_events/freshness_service.py`). Re-use that constant via a small shared module rather than duplicating.

### D5. Migration safety for dev-stage prod
- Migration is **table-create only** — no `INSERT`, no backfill, no triggers, no `ALTER` of existing tables.
- Read path is **snapshot-first with transparent fallback**. If the table is empty (or every row is stale), the response is byte-identical to today's ROB-168 output except for the new `dataState="fallback"` (or `"missing"`) field on `ScreenerFreshness`.
- CLI defaults to `--dry-run` so accidental invocation is a no-op. Operator must pass `--commit` to write.
- No scheduler entry. The PR description explicitly states: "Recurring scheduler / TaskIQ activation deferred — requires separate approval; see follow-up ticket."

### D6. Source attribution
The snapshot row records `source` as `kis` (KR) or `yahoo` (US). This lets a future cross-source comparison (e.g. KIS vs. Polygon) coexist in the same table.

---

## Schema — `invest_screener_snapshots`

```sql
CREATE TABLE public.invest_screener_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    market              VARCHAR(8)    NOT NULL,                       -- 'kr' | 'us'
    symbol              VARCHAR(20)   NOT NULL,                       -- KR: 6-digit; US: ticker
    snapshot_date       DATE          NOT NULL,                       -- trading-date of latest close in window
    latest_close        NUMERIC(20,6) NOT NULL,
    prev_close          NUMERIC(20,6),
    change_amount       NUMERIC(20,6),                                -- latest - prev
    change_rate         NUMERIC(10,4),                                -- (latest - prev) / prev * 100
    consecutive_up_days INTEGER,                                      -- nullable (window < 2 → NULL)
    week_change_rate    NUMERIC(10,4),                                -- (latest - close[-5]) / close[-5] * 100
    closes_window       JSONB         NOT NULL,                       -- last ≤ 10 closes, ASC by date
    daily_volume        BIGINT,
    source              VARCHAR(16)   NOT NULL,                       -- 'kis' | 'yahoo'
    computed_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT uq_invest_screener_snapshots_market_symbol_date
        UNIQUE (market, symbol, snapshot_date),
    CONSTRAINT ck_invest_screener_snapshots_market
        CHECK (market IN ('kr', 'us')),
    CONSTRAINT ck_invest_screener_snapshots_source
        CHECK (source IN ('kis', 'yahoo'))
);

CREATE INDEX ix_invest_screener_snapshots_market_date
    ON public.invest_screener_snapshots (market, snapshot_date DESC);

CREATE INDEX ix_invest_screener_snapshots_market_streak
    ON public.invest_screener_snapshots (market, consecutive_up_days DESC NULLS LAST)
    WHERE consecutive_up_days IS NOT NULL;
```

ORM model lives at `app/models/invest_screener_snapshot.py`. **Singular file name** matches `app/models/manual_holdings.py` / `app/models/order_preview_session.py` convention.

---

## Computation Rules (canonical — implementation must match)

Inputs: `closes_window: list[Decimal]` (ASC by date, length 1..10), `daily_volume: int | None`, `source: str`.

```python
def _consecutive_up_days(closes: list[Decimal]) -> int | None:
    if len(closes) < 2:
        return None
    streak = 0
    # Walk from the tail backward; identical to calculate_consecutive_up_days.
    for current, previous in zip(
        reversed(closes[1:]), reversed(closes[:-1]), strict=False
    ):
        if current > previous:
            streak += 1
            continue
        break
    return streak

def _week_change_rate(closes: list[Decimal]) -> Decimal | None:
    # 5 trading days prior; if window has < 6 elements (need closes[-1] AND closes[-6]), return None.
    if len(closes) < 6:
        return None
    base = closes[-6]
    if base == 0:
        return None
    return (closes[-1] - base) / base * Decimal("100")

def _change_pair(closes: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if len(closes) < 2:
        return None, None
    latest, prev = closes[-1], closes[-2]
    if prev == 0:
        return latest - prev, None
    return latest - prev, (latest - prev) / prev * Decimal("100")
```

`consecutive_up_days` matches the existing `app.services.invest_view_model.screener_service.calculate_consecutive_up_days` logic exactly — confirmed in test `tests/test_invest_screener_snapshots_builder.py::test_streak_matches_view_model`.

---

## Read-Path Wiring (snapshot-first with fallback)

`app/mcp_server/tooling/screening/enrichment.py::_enrich_consecutive_up_days` is the only function we modify. After ROB-168 lands, it currently fans out N OHLCV fetches with a semaphore. We change it to:

1. Compute `today_trading_date(market)` — KR: most recent business day in `Asia/Seoul`; US: most recent in `America/New_York` (helper lives in `app/services/invest_screener_snapshots/freshness.py`, re-uses `pandas.tseries.offsets.BDay` with no holiday calendar — KIS/Yahoo daily candles already collapse holidays into the previous trading-day close, so this is sufficient for the first slice; doc-string flags the future need for a real exchange calendar).
2. Call `repo.get_fresh(market, symbols, on_or_after=today_trading_date)` to bulk-load snapshot rows.
3. For each row in `rows`:
   - If snapshot exists and `len(closes_window) >= 2`: populate `row["consecutive_up_days"]`, `row["week_change_rate"]`, `row["change_rate"]`, `row["change_amount"]`, `row["close"]` (only if not already set), and tag with internal `_screener_snapshot_state="fresh"|"partial"|"stale"`.
   - Else: leave row as-is (existing on-demand fetch falls through; tag `_screener_snapshot_state="missing"`).
4. The remaining (unfilled) rows go through the existing semaphore-bounded `_fetch_ohlcv_for_indicators` loop **unchanged** (behavior identical to ROB-168 today).
5. Aggregate `_screener_snapshot_state` across all rows and stash on the response dict under `_dataState_aggregate` for the view-model to pull into `ScreenerFreshness.dataState`.

**Critical invariant:** if the snapshot table is empty, this function is byte-equivalent to ROB-168's behavior except for the `dataState="missing"` (no rows found) or `dataState="fallback"` (some rows missing, on-demand filled them) on the response.

---

## API Contract Changes (additive)

Single new field on `ScreenerFreshness`:

```python
# app/schemas/invest_screener.py
class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"] = "missing"
```

Default `"missing"` keeps existing tests that construct `ScreenerFreshness` without specifying `dataState` from breaking (they still pass `model_dump()` round-trip). The router populates `dataState` from `_dataState_aggregate` when present.

New read endpoint (mirrors ROB-167's `/trading/api/market-events/coverage`):

```
GET /trading/api/invest-screener/snapshots/coverage?market=kr|us
→ {
    "market": "kr",
    "asOf": "2026-05-10T05:30:00Z",
    "totalSymbolsInUniverse": 2841,           # active KR universe count
    "snapshotsCoveringToday": 217,            # rows where snapshot_date == today_trading_date
    "snapshotsStale": 14,                     # rows older than 36h
    "snapshotsMissing": 2624,                 # universe minus fresh+stale
    "lastComputedAt": "2026-05-10T05:14:11Z",
    "dataState": "stale"                      # worst-of across the universe
}
```

Read-only. Returns 200 even when the table is empty (`snapshotsCoveringToday=0`, `dataState="missing"`).

---

## File Structure (locked before tasks begin)

| File                                                                                | Change |
|-------------------------------------------------------------------------------------|--------|
| `app/models/invest_screener_snapshot.py`                                            | NEW — `InvestScreenerSnapshot` ORM model. |
| `alembic/versions/<hash>_add_invest_screener_snapshots.py`                          | NEW — `op.create_table(...)`, two indexes, two CHECKs, one UNIQUE. |
| `app/services/invest_screener_snapshots/__init__.py`                                | NEW — empty. |
| `app/services/invest_screener_snapshots/repository.py`                              | NEW — `InvestScreenerSnapshotsRepository` (upsert, get_fresh, list_by_market_date, coverage). |
| `app/services/invest_screener_snapshots/builder.py`                                 | NEW — `build_snapshot_for_symbol`, `build_snapshots_for_market`. |
| `app/services/invest_screener_snapshots/freshness.py`                               | NEW — `today_trading_date(market)`, `classify_state(snapshot, now)`, `aggregate_states(states) -> dataState`. Imports `STALE_AFTER_HOURS` from `app.services.market_events.freshness_service`. |
| `app/services/invest_screener_snapshots/coverage_service.py`                        | NEW — backs the coverage endpoint. |
| `app/mcp_server/tooling/screening/enrichment.py`                                    | MODIFY — snapshot-first short-circuit in `_enrich_consecutive_up_days`. |
| `app/services/invest_view_model/screener_service.py`                                | MODIFY — propagate `_dataState_aggregate` from upstream into `ScreenerFreshness.dataState` when present. |
| `app/schemas/invest_screener.py`                                                    | MODIFY — add `dataState` literal field. |
| `app/routers/invest_api.py`                                                         | MODIFY — register `GET /invest/api/screener/snapshots/coverage` (or new `app/routers/invest_screener_snapshots.py` mounted under `/trading/api/invest-screener/snapshots/...`; pick one — see Task 7 for resolution). |
| `scripts/build_invest_screener_snapshots.py`                                        | NEW — CLI; defaults to `--dry-run`. |
| `scripts/diagnose_invest_screener_snapshots.py`                                     | NEW — read-only coverage CLI mirroring `scripts/diagnose_calendar_coverage.py`. |
| `tests/test_invest_screener_snapshots_model.py`                                     | NEW — model + migration round-trip + CHECK constraints. |
| `tests/test_invest_screener_snapshots_repository.py`                                | NEW — upsert idempotency, get_fresh, coverage. |
| `tests/test_invest_screener_snapshots_builder.py`                                   | NEW — derived-metric correctness; streak parity with view-model helper. |
| `tests/test_invest_screener_snapshots_freshness.py`                                 | NEW — `classify_state` and `aggregate_states`. |
| `tests/test_invest_screener_snapshots_enrichment.py`                                | NEW — snapshot-first wiring + fallback parity. |
| `tests/test_invest_screener_snapshots_router.py`                                    | NEW — coverage GET endpoint. |
| `tests/test_invest_screener_snapshots_name_precedence.py`                           | NEW — KR Korean-name precedence assertion. |
| `tests/test_build_invest_screener_snapshots_cli.py`                                 | NEW — CLI smoke (dry-run prints, --commit honored, no broker import). |
| `docs/runbooks/invest-screener-snapshots.md`                                        | NEW — operator runbook (CLI usage, coverage endpoint, fallback semantics, scheduler-deferred note). |

---

## Acceptance Checks (gate the merge)

1. **Migrations clean:** `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` succeeds locally and in CI; `alembic current` matches the new revision.
2. **Test suite green:**
   ```bash
   uv run pytest \
     tests/test_invest_screener_snapshots_model.py \
     tests/test_invest_screener_snapshots_repository.py \
     tests/test_invest_screener_snapshots_builder.py \
     tests/test_invest_screener_snapshots_freshness.py \
     tests/test_invest_screener_snapshots_enrichment.py \
     tests/test_invest_screener_snapshots_router.py \
     tests/test_invest_screener_snapshots_name_precedence.py \
     tests/test_build_invest_screener_snapshots_cli.py \
     tests/test_invest_view_model_screener_service.py \
     tests/test_invest_view_model_safety.py \
     tests/test_invest_screener_schemas.py \
     -q
   ```
   All green; `test_invest_view_model_safety.py` confirms no broker/order modules pulled in transitively.
3. **Lint + types:** `make lint && make typecheck` clean.
4. **Snapshot fallback parity (manual / CI fixture):** With the snapshot table **empty**, `tests/test_invest_screener_snapshots_enrichment.py::test_empty_table_is_byte_equivalent_to_rob_168` confirms response payload (excluding `freshness.dataState`) is byte-equivalent to the ROB-168 baseline captured in `tests/fixtures/screener_consecutive_gainers_rob168.json`.
5. **Production smoke (operator-driven, after merge):**
   - On staging/dev-prod, operator runs:
     ```bash
     uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20 --commit
     uv run python -m scripts.build_invest_screener_snapshots --market us --limit 10 --commit
     uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
     ```
   - `diagnose_invest_screener_snapshots` prints `dataState=fresh` for ≥ 17 of 20 KR symbols (allow ≤ 3 KIS fetch failures).
   - Hit `GET /invest/api/screener/results?presetId=consecutive_gainers&market=kr` and verify `freshness.dataState == "fresh"` (or `"fallback"` if universe > 20 — expected for first slice; document in runbook).
   - Hit `GET /trading/api/invest-screener/snapshots/coverage?market=kr` — returns 200 with `snapshotsCoveringToday >= 17`.
   - Confirm one screener row whose `consecutive_up_days` came from the snapshot exactly matches the value the on-demand path would have computed (run the CLI in `--dry-run` for the same symbol; values must match — covered by `test_streak_matches_view_model`).
6. **Safety audit (manual checklist in PR description):**
   - [ ] No new module under `app.services.brokers.*` mutated, no new `INSERT/UPDATE/DELETE` outside the new repository's upsert.
   - [ ] No scheduler entry added (`grep -r "schedule=" app/tasks` count unchanged from `main`).
   - [ ] CLI default is `--dry-run`; running without flags produces zero DB writes (covered by `test_build_invest_screener_snapshots_cli.py::test_default_is_dry_run`).
   - [ ] Migration only creates one new table; `git diff main -- alembic/versions/` shows exactly one new file.

---

## Tasks

### Task 1: ORM model + migration

**Files:**
- Create: `app/models/invest_screener_snapshot.py`
- Create: `alembic/versions/<NEW-HASH>_add_invest_screener_snapshots.py`
- Test: `tests/test_invest_screener_snapshots_model.py`

- [ ] **Step 1: Write the failing model test**

```python
# tests/test_invest_screener_snapshots_model.py
import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.invest_screener_snapshot import InvestScreenerSnapshot


@pytest.mark.asyncio
async def test_insert_round_trip(async_session):
    snap = InvestScreenerSnapshot(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        prev_close=Decimal("77900"),
        change_amount=Decimal("600"),
        change_rate=Decimal("0.7702"),
        consecutive_up_days=3,
        week_change_rate=Decimal("2.1500"),
        closes_window=[77000, 77400, 77900, 78500],
        daily_volume=14_500_000,
        source="kis",
    )
    async_session.add(snap)
    await async_session.commit()

    result = await async_session.execute(
        sa.select(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol == "005930"
        )
    )
    fetched = result.scalar_one()
    assert fetched.consecutive_up_days == 3
    assert fetched.closes_window == [77000, 77400, 77900, 78500]


@pytest.mark.asyncio
async def test_unique_constraint(async_session):
    base = dict(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        closes_window=[78500],
        source="kis",
    )
    async_session.add(InvestScreenerSnapshot(**base))
    await async_session.commit()

    async_session.add(InvestScreenerSnapshot(**base))
    with pytest.raises(IntegrityError):
        await async_session.commit()


@pytest.mark.asyncio
async def test_market_check_constraint(async_session):
    async_session.add(
        InvestScreenerSnapshot(
            market="crypto",  # invalid
            symbol="BTC",
            snapshot_date=dt.date(2026, 5, 9),
            latest_close=Decimal("100000"),
            closes_window=[100000],
            source="kis",
        )
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()
```

- [ ] **Step 2: Run — should fail (model doesn't exist)**

```bash
uv run pytest tests/test_invest_screener_snapshots_model.py -q
```
Expected: ImportError on `app.models.invest_screener_snapshot`.

- [ ] **Step 3: Create the ORM model**

```python
# app/models/invest_screener_snapshot.py
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InvestScreenerSnapshot(Base):
    __tablename__ = "invest_screener_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            name="uq_invest_screener_snapshots_market_symbol_date",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_invest_screener_snapshots_market",
        ),
        CheckConstraint(
            "source IN ('kis', 'yahoo')",
            name="ck_invest_screener_snapshots_source",
        ),
        Index(
            "ix_invest_screener_snapshots_market_date",
            "market",
            "snapshot_date",
        ),
        Index(
            "ix_invest_screener_snapshots_market_streak",
            "market",
            "consecutive_up_days",
            postgresql_where="consecutive_up_days IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    latest_close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    consecutive_up_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week_change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    closes_window: Mapped[list] = mapped_column(JSONB, nullable=False)
    daily_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 4: Generate migration**

```bash
uv run alembic revision --autogenerate -m "add invest screener snapshots"
```
Then **manually edit** the generated file to:
1. Verify the autogen'd `op.create_table(...)` matches the SQL in the schema section above (CHECK constraints sometimes need manual addition — autogen doesn't always emit them).
2. Add the partial index manually if autogen omits the `postgresql_where` clause:

```python
op.create_index(
    "ix_invest_screener_snapshots_market_streak",
    "invest_screener_snapshots",
    ["market", "consecutive_up_days"],
    postgresql_where=sa.text("consecutive_up_days IS NOT NULL"),
)
```

- [ ] **Step 5: Run upgrade and tests**

```bash
uv run alembic upgrade head
uv run pytest tests/test_invest_screener_snapshots_model.py -q
```
Expected: PASS.

- [ ] **Step 6: Verify rollback works**

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: both succeed; `alembic current` matches new revision.

- [ ] **Step 7: Commit**

```bash
git add app/models/invest_screener_snapshot.py alembic/versions/*invest_screener_snapshots*.py tests/test_invest_screener_snapshots_model.py
git commit -m "feat(MGH-7): add invest_screener_snapshots model and migration"
```

---

### Task 2: Repository

**Files:**
- Create: `app/services/invest_screener_snapshots/__init__.py`
- Create: `app/services/invest_screener_snapshots/repository.py`
- Test: `tests/test_invest_screener_snapshots_repository.py`

- [ ] **Step 1: Write failing repository tests**

```python
# tests/test_invest_screener_snapshots_repository.py
import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates(async_session):
    repo = InvestScreenerSnapshotsRepository(async_session)
    payload = SnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        prev_close=Decimal("77900"),
        change_amount=Decimal("600"),
        change_rate=Decimal("0.7702"),
        consecutive_up_days=3,
        week_change_rate=Decimal("2.15"),
        closes_window=[77000, 77400, 77900, 78500],
        daily_volume=14_500_000,
        source="kis",
    )
    await repo.upsert(payload)
    await async_session.commit()

    payload2 = payload.model_copy(update={"consecutive_up_days": 4})
    await repo.upsert(payload2)
    await async_session.commit()

    rows = await repo.get_fresh(market="kr", symbols=["005930"], on_or_after=dt.date(2026, 5, 9))
    assert len(rows) == 1
    assert rows[0].consecutive_up_days == 4


@pytest.mark.asyncio
async def test_get_fresh_filters_stale(async_session):
    repo = InvestScreenerSnapshotsRepository(async_session)
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="005930", snapshot_date=dt.date(2026, 5, 1),
        latest_close=Decimal("70000"), closes_window=[70000], source="kis",
    ))
    await async_session.commit()

    rows = await repo.get_fresh(market="kr", symbols=["005930"], on_or_after=dt.date(2026, 5, 9))
    assert rows == []


@pytest.mark.asyncio
async def test_coverage_counts(async_session):
    repo = InvestScreenerSnapshotsRepository(async_session)
    today = dt.date(2026, 5, 9)
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="005930", snapshot_date=today,
        latest_close=Decimal("78500"), closes_window=[78500], source="kis",
    ))
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="000660", snapshot_date=dt.date(2026, 5, 1),
        latest_close=Decimal("130000"), closes_window=[130000], source="kis",
    ))
    await async_session.commit()

    cov = await repo.coverage(market="kr", today_trading_date=today)
    assert cov.fresh_count == 1
    assert cov.stale_count == 1
```

- [ ] **Step 2: Run — fails on missing module**

```bash
uv run pytest tests/test_invest_screener_snapshots_repository.py -q
```

- [ ] **Step 3: Implement repository**

```python
# app/services/invest_screener_snapshots/__init__.py
# (empty)
```

```python
# app/services/invest_screener_snapshots/repository.py
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot


class SnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str
    symbol: str
    snapshot_date: dt.date
    latest_close: Decimal
    prev_close: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    consecutive_up_days: int | None = None
    week_change_rate: Decimal | None = None
    closes_window: list[Any] = Field(default_factory=list)
    daily_volume: int | None = None
    source: str


@dataclass(frozen=True)
class CoverageCounts:
    market: str
    today_trading_date: dt.date
    fresh_count: int        # snapshot_date == today_trading_date
    stale_count: int        # snapshot_date < today_trading_date
    last_computed_at: dt.datetime | None


class InvestScreenerSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, payload: SnapshotUpsert) -> None:
        values = payload.model_dump()
        stmt = insert(InvestScreenerSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_screener_snapshots_market_symbol_date",
            set_={
                **{
                    k: stmt.excluded[k]
                    for k in values
                    if k not in {"market", "symbol", "snapshot_date"}
                },
                "updated_at": func.now(),
                "computed_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def get_fresh(
        self,
        *,
        market: str,
        symbols: Iterable[str],
        on_or_after: dt.date,
    ) -> list[InvestScreenerSnapshot]:
        symbols_list = list(symbols)
        if not symbols_list:
            return []
        result = await self._session.execute(
            select(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.symbol.in_(symbols_list),
                InvestScreenerSnapshot.snapshot_date >= on_or_after,
            )
        )
        return list(result.scalars().all())

    async def coverage(
        self, *, market: str, today_trading_date: dt.date
    ) -> CoverageCounts:
        result = await self._session.execute(
            select(
                func.count().filter(
                    InvestScreenerSnapshot.snapshot_date == today_trading_date
                ).label("fresh"),
                func.count().filter(
                    InvestScreenerSnapshot.snapshot_date < today_trading_date
                ).label("stale"),
                func.max(InvestScreenerSnapshot.computed_at).label("last_computed_at"),
            ).where(InvestScreenerSnapshot.market == market)
        )
        row = result.one()
        return CoverageCounts(
            market=market,
            today_trading_date=today_trading_date,
            fresh_count=int(row.fresh or 0),
            stale_count=int(row.stale or 0),
            last_computed_at=row.last_computed_at,
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_invest_screener_snapshots_repository.py -q
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_screener_snapshots/__init__.py app/services/invest_screener_snapshots/repository.py tests/test_invest_screener_snapshots_repository.py
git commit -m "feat(MGH-7): add InvestScreenerSnapshotsRepository with upsert/get_fresh/coverage"
```

---

### Task 3: Builder + freshness helpers

**Files:**
- Create: `app/services/invest_screener_snapshots/freshness.py`
- Create: `app/services/invest_screener_snapshots/builder.py`
- Test: `tests/test_invest_screener_snapshots_freshness.py`
- Test: `tests/test_invest_screener_snapshots_builder.py`

- [ ] **Step 1: Write freshness tests**

```python
# tests/test_invest_screener_snapshots_freshness.py
import datetime as dt

import pytest

from app.services.invest_screener_snapshots.freshness import (
    aggregate_states,
    classify_state,
    today_trading_date,
)


def test_today_trading_date_kr_weekend_rolls_back():
    sat = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)  # Sat
    assert today_trading_date("kr", now=sat) == dt.date(2026, 5, 8)  # Fri


def test_classify_state_fresh_when_window_long_enough():
    snap_date = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    state = classify_state(
        snapshot_date=snap_date,
        computed_at=dt.datetime(2026, 5, 9, 5, 0, tzinfo=dt.UTC),
        closes_window_len=10,
        today_trading_date_value=snap_date,
        now=now,
    )
    assert state == "fresh"


def test_classify_state_partial_short_window():
    snap_date = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    state = classify_state(
        snapshot_date=snap_date,
        computed_at=now,
        closes_window_len=3,
        today_trading_date_value=snap_date,
        now=now,
    )
    assert state == "partial"


def test_classify_state_stale_when_old_or_old_computed():
    today = dt.date(2026, 5, 9)
    now = dt.datetime(2026, 5, 9, 10, 0, tzinfo=dt.UTC)
    # Old snapshot_date.
    assert classify_state(
        snapshot_date=dt.date(2026, 5, 1),
        computed_at=now,
        closes_window_len=10,
        today_trading_date_value=today,
        now=now,
    ) == "stale"
    # Old computed_at (>= 36h).
    assert classify_state(
        snapshot_date=today,
        computed_at=dt.datetime(2026, 5, 7, 10, 0, tzinfo=dt.UTC),
        closes_window_len=10,
        today_trading_date_value=today,
        now=now,
    ) == "stale"


@pytest.mark.parametrize(
    "states,expected",
    [
        (["fresh", "fresh", "fresh"], "fresh"),
        (["fresh", "partial"], "partial"),
        (["fresh", "stale"], "stale"),
        (["fresh", "missing"], "fallback"),
        ([], "missing"),
    ],
)
def test_aggregate_states(states, expected):
    assert aggregate_states(states) == expected
```

- [ ] **Step 2: Run — fails (module missing)**

- [ ] **Step 3: Implement `freshness.py`**

```python
# app/services/invest_screener_snapshots/freshness.py
from __future__ import annotations

import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from app.services.market_events.freshness_service import STALE_AFTER_HOURS

DataState = Literal["fresh", "partial", "stale", "missing", "fallback"]

_TZ_BY_MARKET = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}
_PARTIAL_MAX_LEN = 5  # closes_window length < 5 → partial (week_change_rate not computable)


def today_trading_date(
    market: str, *, now: dt.datetime | None = None
) -> dt.date:
    """Most recent business day in the market's timezone.

    NOTE: First-slice implementation does NOT consult an exchange holiday
    calendar. KIS daily candles already collapse Korean public holidays
    into the previous trading day's close, so this approximation is safe
    for snapshot freshness classification — it only over-marks a row as
    `stale` on the first business day after a holiday cluster, which the
    fallback path handles transparently.
    """
    tz = _TZ_BY_MARKET.get(market, _TZ_BY_MARKET["kr"])
    now_local = (now or dt.datetime.now(dt.UTC)).astimezone(tz)
    candidate = now_local.date()
    while candidate.weekday() >= 5:
        candidate -= dt.timedelta(days=1)
    return candidate


def classify_state(
    *,
    snapshot_date: dt.date,
    computed_at: dt.datetime,
    closes_window_len: int,
    today_trading_date_value: dt.date,
    now: dt.datetime,
) -> DataState:
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    age_hours = (now - computed_at).total_seconds() / 3600.0
    if snapshot_date < today_trading_date_value or age_hours >= STALE_AFTER_HOURS:
        return "stale"
    if closes_window_len < 2:
        return "missing"  # not really a usable row; treat as if absent
    if closes_window_len < _PARTIAL_MAX_LEN:
        return "partial"
    return "fresh"


_PRIORITY: dict[DataState, int] = {
    "missing": 0,
    "fallback": 1,
    "stale": 2,
    "partial": 3,
    "fresh": 4,
}


def aggregate_states(states: list[DataState]) -> DataState:
    if not states:
        return "missing"
    has_missing = "missing" in states
    has_fresh_or_partial = any(s in {"fresh", "partial"} for s in states)
    if has_missing and has_fresh_or_partial:
        return "fallback"
    return min(states, key=lambda s: _PRIORITY[s])
```

- [ ] **Step 4: Write builder tests**

```python
# tests/test_invest_screener_snapshots_builder.py
import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.invest_screener_snapshots.builder import (
    build_snapshot_for_symbol,
    derive_metrics,
)
from app.services.invest_view_model.screener_service import (
    calculate_consecutive_up_days,
)


def test_derive_metrics_full_window():
    closes = [Decimal(c) for c in [100, 101, 102, 103, 104, 105, 106, 107, 108, 110]]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == 9
    assert metrics.latest_close == Decimal("110")
    assert metrics.prev_close == Decimal("108")
    assert metrics.change_amount == Decimal("2")
    assert round(metrics.change_rate, 4) == Decimal("1.8519")
    # week_change uses closes[-6] = 105 → (110-105)/105*100
    assert round(metrics.week_change_rate, 4) == Decimal("4.7619")


def test_derive_metrics_streak_matches_view_model():
    closes = [Decimal(c) for c in [99, 100, 99, 100, 101, 102]]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == calculate_consecutive_up_days(
        [float(c) for c in closes]
    )


def test_derive_metrics_short_window_returns_partial():
    closes = [Decimal("100"), Decimal("101")]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == 1
    assert metrics.week_change_rate is None  # < 6 elements


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_kr(monkeypatch):
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-29", periods=10),
            "close": [100, 101, 102, 103, 104, 105, 106, 107, 108, 110],
            "volume": [1_000_000] * 10,
        }
    )
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        fetcher,
    )

    payload = await build_snapshot_for_symbol(
        market="kr", symbol="005930", today=dt.date(2026, 5, 9)
    )
    assert payload is not None
    assert payload.market == "kr"
    assert payload.symbol == "005930"
    assert payload.snapshot_date == dt.date(2026, 5, 8)  # latest row in df
    assert payload.consecutive_up_days == 9
    assert payload.daily_volume == 1_000_000
    assert payload.source == "kis"
    fetcher.assert_awaited_once_with("005930", "equity_kr", count=10)


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_returns_none_on_empty_df(monkeypatch):
    fetcher = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        fetcher,
    )
    assert (
        await build_snapshot_for_symbol(market="us", symbol="AAPL", today=dt.date.today())
        is None
    )
```

- [ ] **Step 5: Implement builder**

```python
# app/services/invest_screener_snapshots/builder.py
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.mcp_server.tooling.market_data_indicators import _fetch_ohlcv_for_indicators
from app.services.invest_screener_snapshots.repository import SnapshotUpsert

logger = logging.getLogger(__name__)

_LOOKBACK = 10


@dataclass(frozen=True)
class DerivedMetrics:
    latest_close: Decimal
    prev_close: Decimal | None
    change_amount: Decimal | None
    change_rate: Decimal | None
    consecutive_up_days: int | None
    week_change_rate: Decimal | None


def derive_metrics(closes: Sequence[Decimal]) -> DerivedMetrics:
    if not closes:
        raise ValueError("closes must be non-empty")
    latest = Decimal(closes[-1])
    prev = Decimal(closes[-2]) if len(closes) >= 2 else None

    if prev is None:
        change_amount = None
        change_rate = None
    else:
        change_amount = latest - prev
        change_rate = (
            (change_amount / prev * Decimal("100")) if prev != 0 else None
        )

    streak: int | None
    if len(closes) < 2:
        streak = None
    else:
        streak = 0
        for current, previous in zip(
            reversed(list(closes[1:])), reversed(list(closes[:-1])), strict=False
        ):
            if Decimal(current) > Decimal(previous):
                streak += 1
                continue
            break

    if len(closes) >= 6:
        base = Decimal(closes[-6])
        week_change_rate = (
            (latest - base) / base * Decimal("100") if base != 0 else None
        )
    else:
        week_change_rate = None

    return DerivedMetrics(
        latest_close=latest,
        prev_close=prev,
        change_amount=change_amount,
        change_rate=change_rate,
        consecutive_up_days=streak,
        week_change_rate=week_change_rate,
    )


def _market_type_and_source(market: str) -> tuple[str, str]:
    if market == "kr":
        return "equity_kr", "kis"
    if market == "us":
        return "equity_us", "yahoo"
    raise ValueError(f"unsupported market: {market}")


async def build_snapshot_for_symbol(
    *, market: str, symbol: str, today: dt.date
) -> SnapshotUpsert | None:
    market_type, source = _market_type_and_source(market)
    try:
        df = await _fetch_ohlcv_for_indicators(symbol, market_type, count=_LOOKBACK)
    except Exception as exc:  # noqa: BLE001 — per-symbol best-effort
        logger.warning("ohlcv fetch failed market=%s symbol=%s: %s", market, symbol, exc)
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    df = df.sort_values("date").reset_index(drop=True) if "date" in df.columns else df
    closes_raw: list[Any] = list(df["close"].tolist())
    closes = [Decimal(str(c)) for c in closes_raw if c is not None]
    if not closes:
        return None

    metrics = derive_metrics(closes)
    snapshot_date = (
        df["date"].iloc[-1].date()
        if "date" in df.columns
        else today
    )
    daily_volume = (
        int(df["volume"].iloc[-1])
        if "volume" in df.columns and df["volume"].iloc[-1] is not None
        else None
    )

    return SnapshotUpsert(
        market=market,
        symbol=symbol,
        snapshot_date=snapshot_date,
        latest_close=metrics.latest_close,
        prev_close=metrics.prev_close,
        change_amount=metrics.change_amount,
        change_rate=metrics.change_rate,
        consecutive_up_days=metrics.consecutive_up_days,
        week_change_rate=metrics.week_change_rate,
        closes_window=[float(c) for c in closes[-_LOOKBACK:]],
        daily_volume=daily_volume,
        source=source,
    )


async def build_snapshots_for_market(
    *,
    market: str,
    symbols: Iterable[str],
    today: dt.date,
    concurrency: int = 4,
) -> list[SnapshotUpsert]:
    import asyncio

    sem = asyncio.Semaphore(concurrency)
    results: list[SnapshotUpsert | None] = [None] * len(list(symbols))
    symbols_list = list(symbols)

    async def _one(idx: int, sym: str) -> None:
        async with sem:
            results[idx] = await build_snapshot_for_symbol(
                market=market, symbol=sym, today=today
            )

    await asyncio.gather(*(_one(i, s) for i, s in enumerate(symbols_list)))
    return [r for r in results if r is not None]
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_invest_screener_snapshots_freshness.py tests/test_invest_screener_snapshots_builder.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_screener_snapshots/freshness.py app/services/invest_screener_snapshots/builder.py tests/test_invest_screener_snapshots_freshness.py tests/test_invest_screener_snapshots_builder.py
git commit -m "feat(MGH-7): builder + freshness helpers for screener snapshots"
```

---

### Task 4: Read-path wiring (snapshot-first short-circuit)

**Files:**
- Modify: `app/mcp_server/tooling/screening/enrichment.py` — `_enrich_consecutive_up_days`
- Modify: `app/services/invest_view_model/screener_service.py` — surface `dataState` aggregate
- Modify: `app/schemas/invest_screener.py` — add `dataState` field
- Test: `tests/test_invest_screener_snapshots_enrichment.py`

- [ ] **Step 1: Extend `ScreenerFreshness` with `dataState`**

```python
# app/schemas/invest_screener.py — replace existing ScreenerFreshness
class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"] = "missing"
```

Run existing schema tests; they should still pass because `dataState` has a default:

```bash
uv run pytest tests/test_invest_screener_schemas.py -q
```

- [ ] **Step 2: Write the wiring test**

```python
# tests/test_invest_screener_snapshots_enrichment.py
import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.screening import enrichment
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_enrichment_reads_from_snapshot_when_fresh(async_session, monkeypatch):
    repo = InvestScreenerSnapshotsRepository(async_session)
    today = dt.date(2026, 5, 9)
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="005930", snapshot_date=today,
        latest_close=Decimal("78500"), prev_close=Decimal("77900"),
        change_amount=Decimal("600"), change_rate=Decimal("0.7702"),
        consecutive_up_days=4, week_change_rate=Decimal("2.1"),
        closes_window=[77000, 77100, 77400, 77900, 78500],
        source="kis",
    ))
    await async_session.commit()

    fetcher = AsyncMock()
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.freshness.today_trading_date",
        lambda market, now=None: today,
    )

    rows = [{"market": "kr", "code": "005930"}]
    await enrichment._enrich_consecutive_up_days(
        rows, market="kr", session=async_session
    )
    assert rows[0]["consecutive_up_days"] == 4
    assert rows[0]["_screener_snapshot_state"] == "fresh"
    fetcher.assert_not_called()


@pytest.mark.asyncio
async def test_enrichment_falls_back_when_snapshot_missing(async_session, monkeypatch):
    import pandas as pd

    df = pd.DataFrame({
        "date": pd.date_range("2026-04-29", periods=5),
        "close": [100, 101, 102, 103, 104],
    })
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.freshness.today_trading_date",
        lambda market, now=None: dt.date(2026, 5, 9),
    )

    rows = [{"market": "kr", "code": "005930"}]
    await enrichment._enrich_consecutive_up_days(
        rows, market="kr", session=async_session
    )
    assert rows[0]["consecutive_up_days"] == 4
    assert rows[0]["_screener_snapshot_state"] == "missing"
    fetcher.assert_awaited()


@pytest.mark.asyncio
async def test_enrichment_no_session_keeps_rob168_behavior(monkeypatch):
    """When no DB session is provided (legacy callers), behavior matches ROB-168."""
    import pandas as pd
    df = pd.DataFrame({"date": pd.date_range("2026-04-29", periods=5), "close": [1,2,3,4,5]})
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(enrichment, "_fetch_ohlcv_for_indicators", fetcher)

    rows = [{"market": "kr", "code": "005930"}]
    await enrichment._enrich_consecutive_up_days(rows, market="kr")
    assert rows[0]["consecutive_up_days"] == 4
    fetcher.assert_awaited()
```

- [ ] **Step 3: Modify `_enrich_consecutive_up_days`**

The new signature accepts an optional async session. When provided, the snapshot read short-circuits the OHLCV fetch.

```python
# app/mcp_server/tooling/screening/enrichment.py
# (top imports — add)
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invest_screener_snapshots.freshness import (
    classify_state,
    today_trading_date,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)


async def _enrich_consecutive_up_days(
    rows: list[dict[str, Any]],
    *,
    market: str,
    lookback: int = _STREAK_LOOKBACK_DEFAULT,
    session: AsyncSession | None = None,
) -> None:
    if not rows:
        return

    market_type = "equity_kr" if market == "kr" else "equity_us"

    if session is not None and market in {"kr", "us"}:
        await _hydrate_from_snapshots(rows, market=market, session=session)

    sem = asyncio.Semaphore(_STREAK_CONCURRENCY)

    async def _enrich_one(row: dict[str, Any]) -> None:
        if row.get("consecutive_up_days") is not None:
            # Already populated (snapshot-fresh or upstream); nothing to do.
            return
        symbol = _streak_symbol(row)
        if not symbol:
            return
        async with sem:
            try:
                df = await _fetch_ohlcv_for_indicators(
                    symbol, market_type, count=lookback
                )
            except Exception:
                return
        if df is None or df.empty or "close" not in df.columns:
            return
        closes = df["close"].tolist()
        streak = calculate_consecutive_up_days(closes)
        if streak is not None:
            row["consecutive_up_days"] = streak

    await asyncio.gather(*(_enrich_one(r) for r in rows))


async def _hydrate_from_snapshots(
    rows: list[dict[str, Any]], *, market: str, session: AsyncSession
) -> None:
    import datetime as dt

    repo = InvestScreenerSnapshotsRepository(session)
    today = today_trading_date(market)
    symbols = [_streak_symbol(r) for r in rows]
    fetched = await repo.get_fresh(
        market=market, symbols=[s for s in symbols if s], on_or_after=dt.date.min
    )  # pull all rows for these symbols; we'll classify in Python
    by_symbol = {row.symbol: row for row in fetched}

    now = dt.datetime.now(dt.UTC)
    for row in rows:
        sym = _streak_symbol(row)
        snap = by_symbol.get(sym) if sym else None
        if snap is None:
            row["_screener_snapshot_state"] = "missing"
            continue
        state = classify_state(
            snapshot_date=snap.snapshot_date,
            computed_at=snap.computed_at,
            closes_window_len=len(snap.closes_window or []),
            today_trading_date_value=today,
            now=now,
        )
        row["_screener_snapshot_state"] = state
        if state in {"fresh", "partial"}:
            row.setdefault("consecutive_up_days", snap.consecutive_up_days)
            if snap.week_change_rate is not None:
                row.setdefault("week_change_rate", float(snap.week_change_rate))
            if snap.change_rate is not None:
                row.setdefault("change_rate", float(snap.change_rate))
            if snap.change_amount is not None:
                row.setdefault("change_amount", float(snap.change_amount))
            if snap.latest_close is not None:
                row.setdefault("close", float(snap.latest_close))
```

Then update each caller in `app/mcp_server/tooling/screening/kr.py` and `.../us.py` to pass through their existing session (or `None` if absent — the function tolerates it).

- [ ] **Step 4: Aggregate dataState in view-model**

```python
# app/services/invest_view_model/screener_service.py — inside build_screener_results,
# right before constructing ScreenerFreshness:
states = [str(r.get("_screener_snapshot_state") or "missing") for r in rows]
from app.services.invest_screener_snapshots.freshness import aggregate_states
aggregated = aggregate_states(states)  # type: ignore[arg-type]

# In _build_freshness, accept and pass dataState:
freshness = _build_freshness(
    raw_timestamp=raw.get("timestamp"),
    cache_hit=bool(raw.get("cache_hit")),
    market=requested_market,
    now=now,
    dataState=aggregated,
)
```

Update `_build_freshness` signature to accept `dataState: str = "missing"` and forward it into the `ScreenerFreshness(...)` constructor.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_invest_screener_snapshots_enrichment.py tests/test_invest_view_model_screener_service.py tests/test_invest_view_model_safety.py tests/test_invest_screener_schemas.py -q
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/invest_screener.py app/mcp_server/tooling/screening/enrichment.py app/mcp_server/tooling/screening/kr.py app/mcp_server/tooling/screening/us.py app/services/invest_view_model/screener_service.py tests/test_invest_screener_snapshots_enrichment.py
git commit -m "feat(MGH-7): snapshot-first read path with transparent fallback"
```

---

### Task 5: KR Korean-name precedence assertion

**Files:**
- Test: `tests/test_invest_screener_snapshots_name_precedence.py`

This task adds NO production code — it locks in existing behavior so a future refactor of name resolution can't silently regress.

- [ ] **Step 1: Write the test**

```python
# tests/test_invest_screener_snapshots_name_precedence.py
import datetime as dt
from decimal import Decimal

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.invest_view_model.screener_service import build_screener_results
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_kr_row_uses_kr_universe_name_over_upstream(async_session, fake_screening_service, fake_resolver):
    async_session.add(KRSymbolUniverse(
        symbol="005930", name="삼성전자", exchange="KOSPI"
    ))
    repo = InvestScreenerSnapshotsRepository(async_session)
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="005930", snapshot_date=dt.date.today(),
        latest_close=Decimal("78500"),
        closes_window=[77000, 77400, 77900, 78500, 78500],
        consecutive_up_days=2,
        source="kis",
    ))
    await async_session.commit()

    fake_screening_service.set_response({
        "results": [{"symbol": "005930", "market": "kr", "name": "Samsung Electronics", "close": 78500}],
        "timestamp": "2026-05-09T10:00:00Z",
    })
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening_service,
        resolver=fake_resolver,
        market="kr",
    )
    assert response.results[0].name == "삼성전자"


@pytest.mark.asyncio
async def test_us_row_falls_back_to_name_en_when_kr_absent(async_session, fake_screening_service, fake_resolver):
    async_session.add(USSymbolUniverse(
        symbol="AAPL", name_kr=None, name_en="Apple Inc."
    ))
    await async_session.commit()

    fake_screening_service.set_response({
        "results": [{"symbol": "AAPL", "market": "us", "close": 200}],
        "timestamp": "2026-05-09T10:00:00Z",
    })
    response = await build_screener_results(
        preset_id="consecutive_gainers",
        screening_service=fake_screening_service,
        resolver=fake_resolver,
        market="us",
    )
    assert response.results[0].name == "Apple Inc."
```

This test **may fail** initially if the screener view-model does not currently consult `kr_symbol_universe` for names. If so, add a small `name_resolver` step in `app/services/invest_view_model/screener_service.py::build_screener_results` that, before constructing each `ScreenerResultRow`, looks up KR symbols in `kr_symbol_universe` and prefers `name` when the upstream row's `name` is non-Korean (heuristic: `not any('가' <= ch <= '힣' for ch in upstream_name)`). Implementer: if the lookup is non-trivial, escalate to reviewer rather than expanding scope.

- [ ] **Step 2: Run test, fix view-model if needed**

```bash
uv run pytest tests/test_invest_screener_snapshots_name_precedence.py -q
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_invest_screener_snapshots_name_precedence.py app/services/invest_view_model/screener_service.py
git commit -m "feat(MGH-7): assert KR Korean-name precedence in screener view-model"
```

---

### Task 6: CLI builder (`scripts/build_invest_screener_snapshots.py`)

**Files:**
- Create: `scripts/build_invest_screener_snapshots.py`
- Test: `tests/test_build_invest_screener_snapshots_cli.py`

- [ ] **Step 1: Write CLI smoke tests**

```python
# tests/test_build_invest_screener_snapshots_cli.py
import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from scripts import build_invest_screener_snapshots as cli


def test_default_is_dry_run():
    args = cli.parse_args(["--market", "kr", "--limit", "5"])
    assert args.dry_run is True
    assert args.commit is False


def test_commit_flag_negates_dry_run():
    args = cli.parse_args(["--market", "kr", "--commit"])
    assert args.commit is True
    assert args.dry_run is False


@pytest.mark.asyncio
async def test_run_dry_run_produces_no_writes(monkeypatch, async_session):
    from app.services.invest_screener_snapshots.repository import SnapshotUpsert

    monkeypatch.setattr(
        cli, "build_snapshots_for_market", AsyncMock(return_value=[
            SnapshotUpsert(
                market="kr", symbol="005930",
                snapshot_date=dt.date(2026, 5, 9),
                latest_close=Decimal("78500"),
                closes_window=[78500],
                source="kis",
            )
        ])
    )
    monkeypatch.setattr(cli, "_resolve_symbols", AsyncMock(return_value=["005930"]))

    code = await cli.run(cli.parse_args(["--market", "kr", "--limit", "1"]))
    assert code == 0

    from sqlalchemy import select
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    rows = (await async_session.execute(select(InvestScreenerSnapshot))).scalars().all()
    assert rows == []  # no writes in dry-run


def test_no_broker_imports():
    """The CLI must not transitively import broker mutation modules."""
    import sys
    cli_modules = {m for m in sys.modules if "scripts.build_invest_screener_snapshots" in m}
    assert cli_modules  # imported above
    forbidden = {"app.services.brokers.kis.orders", "app.services.brokers.alpaca.orders"}
    assert forbidden.isdisjoint(set(sys.modules))
```

- [ ] **Step 2: Implement the CLI**

```python
#!/usr/bin/env python3
"""Build invest_screener_snapshots rows for an active KR or US universe slice.

DEFAULTS TO --dry-run: prints the SnapshotUpsert payloads it would write,
without committing to the database. Pass --commit to actually persist.

Examples:
    # KR, dry-run, top 20 active universe symbols
    uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

    # KR, persist
    uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20 --commit

    # US explicit symbols, persist
    uv run python -m scripts.build_invest_screener_snapshots \\
        --market us --symbol AAPL --symbol MSFT --commit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, date

import sqlalchemy as sa

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.builder import (
    build_snapshots_for_market,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-mostly invest_screener_snapshots builder (MGH-7)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to specific symbols. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="When --symbol is not given, max active universe symbols to process.",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the database. Default is --dry-run.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    args = parser.parse_args(argv)
    args.dry_run = not args.commit
    return args


async def _resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    if override:
        return override
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse
            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
                .limit(limit)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .order_by(USSymbolUniverse.symbol)
                .limit(limit)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(UTC).date()
    symbols = await _resolve_symbols(args.market, args.symbol, args.limit)
    logger.info("resolved %d symbols for market=%s", len(symbols), args.market)

    payloads = await build_snapshots_for_market(
        market=args.market, symbols=symbols, today=today, concurrency=args.concurrency
    )

    print(
        f"\nbuilt {len(payloads)}/{len(symbols)} snapshots "
        f"(market={args.market}, dry_run={args.dry_run}):"
    )
    for p in payloads[:10]:
        print(
            f"  {p.market}:{p.symbol} {p.snapshot_date} "
            f"close={p.latest_close} streak={p.consecutive_up_days} "
            f"week={p.week_change_rate}"
        )
    if len(payloads) > 10:
        print(f"  ... ({len(payloads) - 10} more)")

    if args.dry_run:
        print("\n--dry-run: no rows written.\n")
        return 0

    async with AsyncSessionLocal() as session:
        repo = InvestScreenerSnapshotsRepository(session)
        for p in payloads:
            await repo.upsert(p)
        await session.commit()
    print(f"\ncommitted {len(payloads)} rows.\n")
    return 0


async def main() -> int:
    setup_logging_and_sentry()
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_build_invest_screener_snapshots_cli.py -q
```
Expected: PASS.

- [ ] **Step 4: Local smoke (with empty DB)**

```bash
uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 2
# Expect: prints "built 0/0 snapshots" or partial if KR universe is populated locally; no writes.
```

- [ ] **Step 5: Commit**

```bash
git add scripts/build_invest_screener_snapshots.py tests/test_build_invest_screener_snapshots_cli.py
git commit -m "feat(MGH-7): operator CLI to build invest_screener_snapshots (dry-run default)"
```

---

### Task 7: Coverage endpoint + diagnostic CLI

**Files:**
- Create: `app/services/invest_screener_snapshots/coverage_service.py`
- Modify: `app/routers/invest_api.py` — register coverage GET
- Create: `scripts/diagnose_invest_screener_snapshots.py`
- Test: `tests/test_invest_screener_snapshots_router.py`

**Resolution of the file-structure note:** add the route to the existing `app/routers/invest_api.py` (closer co-location with screener results). URL: `GET /invest/api/screener/snapshots/coverage` (under the same prefix as `/invest/api/screener/results`).

- [ ] **Step 1: Write the router test**

```python
# tests/test_invest_screener_snapshots_router.py
import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_coverage_endpoint_empty_returns_missing(test_client: TestClient):
    r = test_client.get("/invest/api/screener/snapshots/coverage", params={"market": "kr"})
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "kr"
    assert body["snapshotsCoveringToday"] == 0
    assert body["dataState"] == "missing"


@pytest.mark.asyncio
async def test_coverage_endpoint_reports_fresh(async_session, test_client: TestClient):
    repo = InvestScreenerSnapshotsRepository(async_session)
    today = dt.date.today()
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="005930", snapshot_date=today,
        latest_close=Decimal("78500"),
        closes_window=[77000, 77100, 77400, 77900, 78500],
        source="kis",
    ))
    await async_session.commit()

    r = test_client.get("/invest/api/screener/snapshots/coverage", params={"market": "kr"})
    body = r.json()
    assert body["snapshotsCoveringToday"] == 1
    assert body["dataState"] in {"fresh", "fallback"}  # depends on universe size; both acceptable for first slice
```

- [ ] **Step 2: Implement coverage service**

```python
# app/services/invest_screener_snapshots/coverage_service.py
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.freshness import (
    DataState,
    today_trading_date,
)


@dataclass(frozen=True)
class CoverageReport:
    market: str
    asOf: dt.datetime
    totalSymbolsInUniverse: int
    snapshotsCoveringToday: int
    snapshotsStale: int
    snapshotsMissing: int
    lastComputedAt: dt.datetime | None
    dataState: DataState


async def build_coverage(
    session: AsyncSession, *, market: str
) -> CoverageReport:
    today = today_trading_date(market)
    now = dt.datetime.now(dt.UTC)

    if market == "kr":
        from app.models.kr_symbol_universe import KRSymbolUniverse
        universe_count_stmt = sa.select(sa.func.count()).select_from(
            KRSymbolUniverse
        ).where(KRSymbolUniverse.is_active.is_(True))
    else:
        from app.models.us_symbol_universe import USSymbolUniverse
        universe_count_stmt = sa.select(sa.func.count()).select_from(USSymbolUniverse)

    universe_count = int((await session.execute(universe_count_stmt)).scalar() or 0)

    stmt = sa.select(
        sa.func.count().filter(InvestScreenerSnapshot.snapshot_date == today).label("fresh"),
        sa.func.count().filter(InvestScreenerSnapshot.snapshot_date < today).label("stale"),
        sa.func.max(InvestScreenerSnapshot.computed_at).label("last"),
    ).where(InvestScreenerSnapshot.market == market)
    row = (await session.execute(stmt)).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    missing = max(0, universe_count - fresh - stale)

    if fresh == 0 and stale == 0:
        state: DataState = "missing"
    elif missing > 0 and fresh > 0:
        state = "fallback"
    elif stale > 0 and fresh == 0:
        state = "stale"
    elif stale > 0:
        state = "stale"  # mixed fresh+stale: worst-of
    else:
        state = "fresh"

    return CoverageReport(
        market=market,
        asOf=now,
        totalSymbolsInUniverse=universe_count,
        snapshotsCoveringToday=fresh,
        snapshotsStale=stale,
        snapshotsMissing=missing,
        lastComputedAt=row.last,
        dataState=state,
    )
```

- [ ] **Step 3: Register the route**

```python
# app/routers/invest_api.py — add near other screener endpoints
from app.services.invest_screener_snapshots.coverage_service import build_coverage

@router.get("/invest/api/screener/snapshots/coverage")
async def screener_snapshots_coverage(
    market: Literal["kr", "us"] = Query("kr"),
    session: AsyncSession = Depends(get_async_session),
):
    report = await build_coverage(session, market=market)
    return {
        "market": report.market,
        "asOf": report.asOf.isoformat(),
        "totalSymbolsInUniverse": report.totalSymbolsInUniverse,
        "snapshotsCoveringToday": report.snapshotsCoveringToday,
        "snapshotsStale": report.snapshotsStale,
        "snapshotsMissing": report.snapshotsMissing,
        "lastComputedAt": report.lastComputedAt.isoformat() if report.lastComputedAt else None,
        "dataState": report.dataState,
    }
```

(Implementer: confirm the exact `Depends` / session helper used elsewhere in `app/routers/invest_api.py` and mirror it.)

- [ ] **Step 4: Diagnostic CLI**

```python
# scripts/diagnose_invest_screener_snapshots.py
"""Read-only invest_screener_snapshots coverage diagnostic CLI (MGH-7).

NEVER writes to the database. Mirrors scripts/diagnose_calendar_coverage.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.coverage_service import build_coverage

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only invest_screener_snapshots coverage CLI (MGH-7)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    return parser.parse_args(argv)


async def main() -> int:
    setup_logging_and_sentry()
    args = parse_args()
    async with AsyncSessionLocal() as session:
        report = await build_coverage(session, market=args.market)
    print(
        f"\nmarket={report.market} asOf={report.asOf.isoformat()}\n"
        f"  universe={report.totalSymbolsInUniverse}\n"
        f"  coveringToday={report.snapshotsCoveringToday}\n"
        f"  stale={report.snapshotsStale}\n"
        f"  missing={report.snapshotsMissing}\n"
        f"  lastComputedAt={report.lastComputedAt}\n"
        f"  dataState={report.dataState}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_invest_screener_snapshots_router.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_screener_snapshots/coverage_service.py app/routers/invest_api.py scripts/diagnose_invest_screener_snapshots.py tests/test_invest_screener_snapshots_router.py
git commit -m "feat(MGH-7): coverage endpoint + diagnostic CLI for snapshots"
```

---

### Task 8: Runbook + PR

**Files:**
- Create: `docs/runbooks/invest-screener-snapshots.md`

- [ ] **Step 1: Write the runbook**

Sections (each ≤ 200 words):
1. **Purpose** — what the table backs, what calls it, what the read-fallback semantics are.
2. **Operator workflow** — exact CLI commands for KR + US dry-run, then commit.
3. **Coverage check** — `GET /invest/api/screener/snapshots/coverage?market=kr` and `scripts/diagnose_invest_screener_snapshots.py` usage.
4. **Fallback semantics** — what each `dataState` value means, what the user sees in `/invest/screener` when state is `fallback`.
5. **Scheduler-deferred note** — explicit "no recurring scheduler entry yet; requires separate approval" with a link to the follow-up Linear ticket placeholder.
6. **Safety boundary** — read/model/UI/data-layer only; no broker, order, watch, or order-intent mutations.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/invest-screener-snapshots.md
git commit -m "docs(MGH-7): operator runbook for invest_screener_snapshots"
```

- [ ] **Step 3: Open PR**

Title: `feat(MGH-7): invest_screener_snapshots — snapshot-backed Toss screener slice`

Body must include:
- Linear link.
- Linkback to ROB-168 ("layered on top of").
- The Acceptance Checks list copied verbatim with each box ticked.
- The Safety Audit checklist (D5) copied verbatim with each box ticked.
- Link to the runbook.
- Explicit "Scheduler activation deferred — not enabled in this PR; requires separate approval."
- Production smoke evidence (`diagnose_invest_screener_snapshots --market kr` output, coverage endpoint curl, screenshot of `/invest/screener` showing freshness label with `dataState`).

---

## Self-review (run before opening PR)

1. **Spec coverage:** every requirement from the K0 task body has a task — schema (Task 1), derived metrics (Task 3), read-path with fallback (Task 4), KR Korean-name precedence (Task 5), migration/backfill safety (Tasks 1, 6, D5), acceptance tests + smoke (all tasks + Acceptance Checks section).
2. **Placeholder scan:** no TBD / TODO / "implement later" / "similar to Task N" — every step shows the actual code or command.
3. **Type consistency:** `SnapshotUpsert` fields used in Task 2 match those built in Task 3 (`derive_metrics` → `build_snapshot_for_symbol` → `SnapshotUpsert(...)`); `DataState` literal is identical in `freshness.py` (Task 3), `ScreenerFreshness` (Task 4), and `CoverageReport` (Task 7); `_screener_snapshot_state` keys flow consistently from `_hydrate_from_snapshots` (Task 4) → `aggregate_states` (Task 4 view-model wiring).
4. **Migration path:** Task 1 generates revision off `main`'s current head (auto-detected by alembic autogenerate against the latest existing revision in `alembic/versions/`); implementer must re-run `uv run alembic revision --autogenerate` if main has advanced before opening PR.

---

## Handoff for K1

This plan covers the entire MGH-7 first slice (one PR). The K1 implementer should:
1. Open the worktree at `/Users/mgh3326/worktrees/auto_trader/MGH-7-invest-screener-snapshots`.
2. Confirm ROB-168 is merged into main and rebase if needed.
3. Execute tasks 1–8 in order using the `superpowers:subagent-driven-development` flow (one subagent per task, review between).
4. Report back:
   - All 9 acceptance checks pass (8 numbered + safety audit).
   - PR URL.
   - Coverage endpoint output for KR + US after operator-run smoke.
5. Stop after the PR is open. Do **not** activate any scheduler / TaskIQ entry — that requires a separate K0 ticket and approval.
