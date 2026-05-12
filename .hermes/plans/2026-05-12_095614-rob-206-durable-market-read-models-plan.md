# ROB-206 plan: durable quote / OHLCV / valuation read-model coverage for /invest

Planner: planner (Hermes Kanban K0)
Date: 2026-05-12 09:56 KST
Workspace: `/Users/mgh3326/worktrees/auto_trader/rob-206-durable-market-read-models`
Branch: `feature/rob-206-durable-market-read-models`
Linear: ROB-206 (https://linear.app/mgh3326/issue/ROB-206/auto-trader-durable-quoteohlcvvaluation-read-model-coverage-for-invest)
Parent sprint: ROB-189

Actual model/runtime note: Hermes planner agent ran in this worktree using Claude Opus 4.7 per role/model preference. Recorded for handoff.

## 1. Goal and acceptance criteria

ROB-206 must move the three `provider_unwired` surfaces (`quotes`, `ohlcv`, `valuation_fundamentals`) of `/invest/api/coverage` onto **durable database read-models** so the dashboard can stop relying on request-time KIS/Yahoo/Naver scraping for parity-tier diagnostics.

Acceptance criteria from Linear, restated:

1. After K1 lands and dry-run/backfill packets are approved, `GET /trading/api/invest/coverage` reports `quotes`, `ohlcv`, and `valuation_fundamentals` as one of `fresh / partial / stale / missing` per supported market, **never** `provider_unwired`. Crypto OHLCV / valuation may remain explicitly `unsupported` if no durable read-model is added in this slice — that decision must be documented in the surface `notes`.
2. New durable tables/migrations have unique constraints, indexes for date-range queries, are alembic-revisioned, and have rollback/down notes in the approval packet.
3. Coverage/source-of-truth metadata is correct: `sourceOfTruth` is the new table name (or existing hypertable for OHLCV), not the upstream provider.
4. Tests cover model + repository + freshness service + coverage integration.
5. Builder/job/CLI all default to dry-run, never write to DB without `--commit` + explicit operator approval, and never auto-attach a recurring `schedule=[...]`.
6. No broker / order / watch / order-intent / paper-trade side effects from any K1 code or test. No live request-path scraping is added to `/invest/api/*`.

## 2. Branch / base state found during K0

This worktree is a stale local branch:

- `HEAD` is `82fca64c` (pre-ROB-201). It is **behind** `github/main` (`5beabef3`) by all of ROB-201..ROB-205.
- `app/services/invest_coverage_service.py`, `app/schemas/invest_coverage.py`, `app/models/investor_flow_snapshot.py`, `app/models/invest_screener_snapshot.py` do **not** exist locally; they exist on `github/main`.
- `origin` points at the local mirror `/Users/mgh3326/work/auto_trader`; the real upstream is the `github` remote.

**Mandatory K1 prep step before any new code:**

```bash
cd /Users/mgh3326/worktrees/auto_trader/rob-206-durable-market-read-models
git fetch github main
git rebase github/main      # or: git reset --keep github/main if the branch has no work yet
git status                  # must be clean
```

The branch currently has **no commits ahead of github/main** that we must preserve, so a reset/replant is safe. If a future commit is added before rebase, prefer rebase to retain authorship.

K1 must not push until rebased onto `github/main` so that the new code can `import` the existing `invest_coverage_service` / `invest_coverage` schemas without redefining them.

## 3. Current upstream state (from github/main reconnaissance)

### 3.1 OHLCV — already partially durable

Already durable on `github/main`:

- KR: `public.kr_candles_1m` TimescaleDB hypertable + continuous aggregates `kr_candles_5m / 15m / 30m / 1h` (`scripts/sql/kr_candles_timescale.sql`, alembic `87541fdbc954`, retention `d31f0a2b4c6d`).
- US: `public.us_candles_1m` hypertable + continuous aggregates (`scripts/sql/us_candles_timescale.sql`, alembic `e7a5b7c9d1f2`, retention `f8b6c4d2e1a3`).
- Sync code: `app/services/kr_candles_sync_service.py`, `app/services/us_candles_sync_service.py`, jobs `app/jobs/kr_candles.py`, `app/jobs/us_candles.py`, CLIs `scripts/sync_kr_candles.py`, `scripts/sync_us_candles.py`.

Not durable yet:

- No crypto OHLCV hypertable. `app/services/upbit_ohlcv_cache.py` is Redis-TTL only.
- `app/services/invest_coverage_service.py::_provider_unwired_surfaces` still lists `ohlcv` as a static `provider_unwired` surface; it does **not** query `kr_candles_1m` / `us_candles_1m`.

### 3.2 Quotes — no durable read-model

- `app/services/invest_quote_service.py` exists but issues live `MarketDataClient.inquire_price` / `inquire_overseas_daily_price` per call (request-time KIS scraping); it does not persist anything.
- `app/services/market_data/service.py` returns a provider-bound `Quote` dataclass (`app/services/market_data/contracts.py`) directly from KIS / Yahoo / Upbit; nothing is stored.
- Coverage marks `quotes` as `provider_unwired` with a static `naver_finance` request-time-only candidate for KR only.

### 3.3 Valuation / fundamentals — no durable read-model

- `app/services/naver_finance/valuation.py` scrapes Naver every call.
- `app/mcp_server/tooling/fundamentals/_valuation.py` proxies to Naver or `yfinance` per request.
- Coverage marks `valuation_fundamentals` as `provider_unwired` with a static `naver_finance` request-time-only candidate for KR only.

### 3.4 Patterns to follow

K1 should copy the dry-run-first foundation pattern that ROB-204 (screener snapshots) and ROB-205 (investor flow snapshots) already established:

- `app/models/<surface>_snapshot.py` (ORM with unique/check/index constraints)
- `alembic/versions/<hash>_add_<surface>_snapshots.py`
- `app/services/<surface>_snapshots/repository.py` (Pydantic upsert payload + `InsertOnConflict` upsert)
- `app/services/<surface>_snapshots/freshness.py` (state classifier) or extension of the existing `app/services/invest_screener_snapshots/freshness.py`
- `app/services/<surface>_snapshots/builder.py` (async, injectable fetcher, returns payload + warnings)
- `app/jobs/<surface>_snapshots.py` (dry-run-first job runner with idempotency classification)
- `scripts/build_<surface>_snapshots.py` (CLI defaulting to dry-run)
- `app/tasks/<surface>_snapshot_tasks.py` (TaskIQ wrapper, no `schedule=[...]`)
- Coverage integration in `app/services/invest_coverage_service.py` + schema notes
- Tests under `tests/test_<surface>_*.py`

## 4. K1 deliverable shape (scope decision)

ROB-206 covers three surfaces. To keep K1 reviewable, **bundle the foundation, not the data**:

- K1 produces **one PR** that adds three thin foundations (quotes, valuation_fundamentals) + one wiring change (ohlcv → existing hypertables) + coverage integration.
- K1 commits **no production data**. After merge the surfaces will transition from `provider_unwired` to `missing` (or `fresh` for KR/US OHLCV if hypertables already hold data) until separate approved backfills run.
- Crypto for `ohlcv` and `valuation_fundamentals` is explicitly marked `unsupported` in K1 with a documented follow-up.
- Crypto for `quotes` is included because Upbit `Quote` is cheap and durable storage is symmetrical with KR/US.

If K1 finds the scope is still too large for one PR after spike, the secondary cut-line is:

1. PR-A: OHLCV coverage wiring only (low risk, no new tables, no migration).
2. PR-B: quotes durable read-model + coverage wiring.
3. PR-C: valuation/fundamentals durable read-model + coverage wiring.

PR-A is unblocking; PR-B and PR-C are independent and can land in either order. Default plan is the single-PR bundle; the K2 reviewer may request the split.

## 5. Concrete file plan

### 5.1 Stage A — OHLCV coverage wiring (no new tables)

#### A.1 Add freshness helpers

Create `app/services/market_data_coverage/__init__.py` and `app/services/market_data_coverage/ohlcv_freshness.py`:

```python
# app/services/market_data_coverage/ohlcv_freshness.py
from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_coverage import CoverageState


async def kr_candles_freshness(
    db: AsyncSession,
    *,
    trading_day: dt.date,
    expected_symbols: int | None = None,
) -> tuple[CoverageState, dt.datetime | None, dt.date | None, int, int]:
    """Return (state, latest_time, latest_date, fresh_symbols, stale_symbols) for kr_candles_1m."""
    row = (
        await db.execute(
            sa.text(
                """
                SELECT MAX(time) AS latest_time,
                       MAX(time::date) AS latest_date,
                       COUNT(DISTINCT symbol) FILTER (
                         WHERE time::date >= :trading_day
                       ) AS fresh_symbols,
                       COUNT(DISTINCT symbol) FILTER (
                         WHERE time::date <  :trading_day
                       ) AS stale_symbols
                FROM public.kr_candles_1m
                """
            ),
            {"trading_day": trading_day},
        )
    ).one()
    fresh = int(row.fresh_symbols or 0)
    stale = int(row.stale_symbols or 0)
    if fresh + stale == 0:
        return ("missing", None, None, 0, 0)
    if expected_symbols and fresh < expected_symbols:
        return ("partial" if fresh > 0 else "stale", row.latest_time, row.latest_date, fresh, stale)
    if stale > 0 and fresh == 0:
        return ("stale", row.latest_time, row.latest_date, fresh, stale)
    return ("fresh", row.latest_time, row.latest_date, fresh, stale)


async def us_candles_freshness(...) -> ...:
    """Same shape against public.us_candles_1m."""
```

Notes:
- Use raw `sa.text(...)` because `kr_candles_1m` / `us_candles_1m` are hypertables defined in pure SQL, not in `app/models/*`. Do not add a SQLAlchemy ORM model for them in K1; the candles_sync_service uses `text()` upsert already and changing that is out of scope.
- `expected_symbols` is informational only; if `None`, treat 0 as missing and anything else as fresh/stale by date.
- Crypto branch must not query `kr_candles_*` / `us_candles_*`. Return `("unsupported", None, None, 0, 0)`.

#### A.2 Add `_ohlcv_surfaces()` to `invest_coverage_service`

Edit `app/services/invest_coverage_service.py` on the rebased branch:

- Add `_ohlcv_surfaces(db, market_norm, trading_day)` near `_orderbook_nxt_surfaces`.
- For `m in {"kr", "us"}`: call the corresponding freshness helper, build `InvestCoverageSurface(surface="ohlcv", label="OHLCV candles", market=m, state=..., sourceOfTruth="kr_candles_1m" or "us_candles_1m", latestAt=..., latestDate=..., counts=InvestCoverageCounts(fresh=..., stale=..., total=fresh+stale))`.
- For `m == "crypto"`: `state="unsupported", sourceOfTruth="read_model_gap"`, warning `"Crypto OHLCV durable read-model is not wired; tracked as ROB-206 follow-up."`, no source candidates.
- Insert `surfaces.extend(await _ohlcv_surfaces(db, market_norm, trading_day))` in `build_invest_coverage()` after `_orderbook_nxt_surfaces`.
- Remove `"ohlcv"` from the loop in `_provider_unwired_surfaces()`; leave `quotes` and `valuation_fundamentals` for Stages B/C.

#### A.3 Update `_SURFACE_QUEUE`

In `_SURFACE_QUEUE`, change `"ohlcv": "provider-contract"` → `"ohlcv": "kr-candles-sync"` (with note in comment that this is the durable read-model queue; the actual sync service is already production-active).

`_SCHEDULER_QUEUES` should **not** include `kr-candles-sync` until a separate operator approval — keep it out so the actionability does not falsely demand a scheduler activation gate.

### 5.2 Stage B — Quotes durable read-model

#### B.1 Model

Create `app/models/market_quote_snapshot.py`:

```python
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP, BigInteger, CheckConstraint, Index, Numeric, String,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketQuoteSnapshot(Base):
    __tablename__ = "market_quote_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market", "symbol", "source", "snapshot_at",
            name="uq_market_quote_snapshots_market_symbol_source_at",
        ),
        CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_market_quote_snapshots_market",
        ),
        CheckConstraint(
            "source IN ('kis', 'yahoo', 'upbit', 'naver_finance')",
            name="ck_market_quote_snapshots_source",
        ),
        Index(
            "ix_market_quote_snapshots_market_symbol_at",
            "market", "symbol", "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
```

Design decisions:
- Multiple sources allowed per `(market, symbol)` (KR can have both `kis` and `naver_finance`), uniqueness includes `source` + `snapshot_at`.
- `snapshot_at` is timestamped to the second (not date) because quotes are intra-day. Freshness compares against `now() - INTERVAL` not against `snapshot_date`.
- `raw_payload` is JSONB for forensic debugging only. Builder must redact secret-like keys before persisting (use existing `_redact_sensitive_keys` from `app/services/market_events/normalizers.py` or copy that helper into a shared util).
- No `unique` on `(market, symbol)` because we want a history. The repository deletes/upserts only by the 4-tuple.

#### B.2 Migration

Create `alembic/versions/<hash>_add_market_quote_snapshots.py` with autogenerate. Required in down-revision: latest head on `github/main` (currently the ROB-205 head `9f1a2b3c4d5e_add_investor_flow_snapshots`). Run:

```bash
uv run alembic revision --autogenerate -m "add market_quote_snapshots (ROB-206)"
```

K1 must hand-verify the generated migration:

- `create_table("market_quote_snapshots", ...)` matches the model exactly.
- Indexes/unique constraints present.
- `down_revision` points at the most recent head from `alembic history`.
- `downgrade()` drops the table cleanly; rollback note: dropping table loses any rows previously written by an approved `--commit` job; restore from a backup if needed.

#### B.3 Repository

Create `app/services/market_quote_snapshots/__init__.py` and `app/services/market_quote_snapshots/repository.py`:

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_quote_snapshot import MarketQuoteSnapshot


class MarketQuoteSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str
    symbol: str
    source: str
    snapshot_at: datetime
    price: Decimal
    previous_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None
    raw_payload: dict | None = None


class MarketQuoteSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[MarketQuoteSnapshotUpsert]) -> int:
        payload = [r.model_dump() for r in rows]
        if not payload:
            return 0
        stmt = (
            insert(MarketQuoteSnapshot)
            .values(payload)
            .on_conflict_do_update(
                index_elements=["market", "symbol", "source", "snapshot_at"],
                set_={
                    "price": insert(MarketQuoteSnapshot).excluded.price,
                    "previous_close": insert(MarketQuoteSnapshot).excluded.previous_close,
                    "open": insert(MarketQuoteSnapshot).excluded.open,
                    "high": insert(MarketQuoteSnapshot).excluded.high,
                    "low": insert(MarketQuoteSnapshot).excluded.low,
                    "volume": insert(MarketQuoteSnapshot).excluded.volume,
                    "raw_payload": insert(MarketQuoteSnapshot).excluded.raw_payload,
                },
            )
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def coverage_counts(
        self, market: str, *, fresh_after: datetime
    ) -> tuple[int, int, datetime | None]:
        """Return (fresh_symbol_count, stale_symbol_count, latest_snapshot_at)."""
        ...
```

Use existing patterns from `app/services/invest_screener_snapshots/repository.py` and `app/services/investor_flow_snapshots/repository.py` to keep style consistent.

#### B.4 Freshness service

Create `app/services/market_quote_snapshots/freshness.py`:

- `kr_freshness_window_minutes = 30` (configurable per-market constant, not env var, to keep behavior deterministic in tests).
- `us_freshness_window_minutes = 30`.
- `crypto_freshness_window_minutes = 10`.
- Function `quote_state(market, latest_at, now)`:
  - `missing` if `latest_at is None`
  - `fresh` if `now - latest_at <= window`
  - `stale` if `now - latest_at > window`

Tests must inject `now` explicitly to be deterministic.

#### B.5 Builder

Create `app/services/market_quote_snapshots/builder.py`:

- Accept `symbols`, `market`, optional `fetcher` (default to a thin wrapper over `app.services.market_data.service.get_quote()` — provider boundary).
- For each symbol, call fetcher, convert `Quote` → `MarketQuoteSnapshotUpsert` with `snapshot_at=now` (or whatever provider returns, but never future).
- Bound parallelism with `asyncio.Semaphore`.
- Return `MarketQuoteBuildResult(payloads, warnings)` (frozen dataclass).
- Skip rows whose `price` is `None`, log warning.
- Do **not** touch broker mutating endpoints; only `get_quote` (read).

Important constraint: tests must inject a stub fetcher and never reach `kis_client` / `upbit_client` for real network calls.

#### B.6 Job runner

Create `app/jobs/market_quote_snapshots.py`, mirroring `app/jobs/invest_screener_snapshots.py`:

```python
@dataclass(frozen=True)
class QuoteSnapshotBuildRequest:
    market: str               # 'kr' | 'us' | 'crypto'
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    commit: bool = False
    now: dt.datetime | None = None

@dataclass(frozen=True)
class QuoteSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    idempotency: dict[str, int]   # {"wouldInsert": ..., "wouldUpdate": ..., "duplicatePayloadKeys": ...}
    samples: tuple[QuoteSnapshotSample, ...]
    warnings: tuple[str, ...]
```

Required behavior:

- `_validate_market()` allows `kr`, `us`, `crypto`. Rejects others.
- `resolve_active_universe()` uses `KRSymbolUniverse / USSymbolUniverse / UpbitSymbolUniverse` (active only).
- Dry-run (`commit=False`) **must not** call `repo.upsert()` or `session.commit()`.
- Idempotency precheck: query `(market, symbol, source, snapshot_at_truncated_to_second)` existence and classify.
- Commit mode upserts per-batch.
- Sample is bounded to 10 rows; redact JSONB raw_payload in samples.

#### B.7 CLI

Create `scripts/build_market_quote_snapshots.py`. Defaults to dry-run; supports `--market kr|us|crypto`, `--symbol`, `--limit`, `--all`, `--batch-size`, `--concurrency`, `--commit`, `--now ISO8601` (for tests). Mirror the docstring/`parse_args` style of `scripts/build_invest_screener_snapshots.py`.

#### B.8 Task wrapper (manual, unscheduled)

Create `app/tasks/market_quote_snapshot_tasks.py`. Add `@broker.task(task_name="build_market_quote_snapshots")` wrapping the job. **Do not** attach `schedule=[...]`. Update `app/tasks/__init__.py` to import the module so workers see it.

#### B.9 Coverage integration

Edit `app/services/invest_coverage_service.py`:

- Add `_quote_surfaces(db, market_norm, now)` that, per market, queries `MarketQuoteSnapshotsRepository.coverage_counts()` and uses `quote_state()` to compute state.
- `sourceOfTruth="market_quote_snapshots"`. Source candidates: keep the existing `naver_finance` static candidate for KR with `readiness="request_time_only"` (it remains a fallback signal).
- Insert `surfaces.extend(await _quote_surfaces(db, market_norm, now))` in `build_invest_coverage()`.
- Drop `quotes` from `_provider_unwired_surfaces()` loop.
- `_SURFACE_QUEUE["quotes"] = "market-quote-snapshots"`. Add `"market-quote-snapshots"` to `_SCHEDULER_QUEUES` **only after** an approved scheduler activation — for K1, leave it out so `actionability.approvalGates` only includes `production_db_write_approval` for missing/stale/partial states (matches ROB-204/205 pattern for not-yet-scheduled surfaces). Documented in comment.

### 5.3 Stage C — Valuation/fundamentals durable read-model

#### C.1 Model

Create `app/models/market_valuation_snapshot.py`:

```python
class MarketValuationSnapshot(Base):
    __tablename__ = "market_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint("market", "symbol", "snapshot_date", "source",
                         name="uq_market_valuation_snapshots_market_symbol_date_source"),
        CheckConstraint("market IN ('kr', 'us')",
                        name="ck_market_valuation_snapshots_market"),
        CheckConstraint("source IN ('naver_finance', 'yahoo')",
                        name="ck_market_valuation_snapshots_source"),
        Index("ix_market_valuation_snapshots_market_date",
              "market", "snapshot_date"),
    )
    id: Mapped[int]  # BigInteger PK
    market: Mapped[str]
    symbol: Mapped[str]
    snapshot_date: Mapped[date]
    source: Mapped[str]
    per: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2))
    high_52w: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low_52w: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True),
                                                  server_default=func.now())
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

Notes:
- Date-grained, not timestamp-grained: PER/PBR/ROE move slowly; one row per day per source is enough.
- Crypto explicitly excluded by check constraint — coverage will mark crypto valuation as `unsupported` with note.

#### C.2 Migration / repository / freshness / builder / job / CLI / task

Apply the same skeleton as Stage B with files under `app/services/market_valuation_snapshots/`. Freshness window is daily (today_trading_date pattern from `invest_screener_snapshots.freshness`). Source choice per market: `naver_finance` for KR (re-use `app/services/naver_finance/valuation.py::_parse_valuation_from_soups`), `yahoo` for US (re-use `app/services/brokers/yahoo/client.fetch_fast_info`).

Builder must wrap provider calls in retry-bounded `httpx.AsyncClient` already used by these modules; **do not** add new outbound HTTP integrations in K1.

Coverage integration: `_valuation_surfaces(db, market_norm, trading_day)`. Crypto state `unsupported` with note `"Valuation/fundamentals are not defined for crypto in the /invest contract."`.

`_SURFACE_QUEUE["valuation_fundamentals"] = "market-valuation-snapshots"`. Same scheduler-activation deferral as Stage B.

### 5.4 Schema / response considerations

No required schema changes to `app/schemas/invest_coverage.py` because the existing `InvestCoverageSurface` shape already supports all needed fields (state, sourceOfTruth, counts, latestAt/Date, sourceCandidates, actionability).

Verify after Stage A/B/C that `provider_unwired` is no longer emitted for kr/us in any of the three surfaces (crypto can still be `unsupported`).

## 6. Test plan for K1

Add/extend tests before or alongside implementation:

### 6.1 OHLCV (Stage A)

New file `tests/test_invest_coverage_ohlcv.py`:

- Seed `public.kr_candles_1m` via raw `text()` insert in a test fixture (the existing `tests/test_kr_candles_sync.py` already shows how).
- Cases: empty table → `missing`; latest within today → `fresh`; latest older than trading day → `stale`; some symbols today and some older with `expected_symbols` → `partial`.
- `crypto` → `unsupported` with note string asserted.
- Negative: no scheduler-activation gate on the OHLCV surface unless state is partial/missing/stale (matches actionability for non-scheduler queue).

### 6.2 Quotes (Stage B)

- `tests/test_market_quote_snapshot_model.py` — model imports, constraints exist.
- `tests/test_market_quote_snapshots_repository.py` — upsert idempotency (insert vs update), coverage_counts.
- `tests/test_market_quote_snapshot_freshness.py` — windows for kr/us/crypto with injected `now`.
- `tests/test_market_quote_snapshot_builder.py` — stub fetcher, build payloads with redacted raw_payload.
- `tests/test_market_quote_snapshot_job.py` — dry-run vs commit, idempotency reporting, non-supported market rejection.
- `tests/test_build_market_quote_snapshots_cli.py` — `parse_args` defaults; `--commit` flips flag; `--all` and `--symbol` are exclusive.
- `tests/test_market_quote_snapshot_tasks.py` — task default `commit=False`; static assert no `schedule=[...]` attached.
- `tests/test_invest_coverage.py` — extend with `test_quotes_surface_uses_market_quote_snapshots`:
  - Seed 1 fresh and 1 stale row → surface state `partial`, actionability queue `market-quote-snapshots`, gates `["production_db_write_approval"]` (no scheduler gate yet).
  - Empty table → state `missing`.
  - Naver candidate still present for KR with `readiness="request_time_only"`.

### 6.3 Valuation (Stage C)

Mirror Stage B test inventory under `tests/test_market_valuation_snapshot_*.py` and add:

- `tests/test_invest_coverage_valuation.py` — fresh/stale/missing seeding; crypto `unsupported`; naver static candidate retained for KR.

### 6.4 Static / safety tests

- `tests/test_invest_api_router_safety.py` already exists; verify it still passes (new services must not pull `app.services.kis*` into `invest_api` import graph).
- Add a regression test (if not already present) asserting that `_provider_unwired_surfaces()` no longer returns `quotes` / `ohlcv` / `valuation_fundamentals` for `kr` / `us`.

### 6.5 Commands K1 must run before review

```bash
uv run --group test pytest \
  tests/test_invest_coverage.py \
  tests/test_invest_coverage_ohlcv.py \
  tests/test_market_quote_snapshot_model.py \
  tests/test_market_quote_snapshots_repository.py \
  tests/test_market_quote_snapshot_freshness.py \
  tests/test_market_quote_snapshot_builder.py \
  tests/test_market_quote_snapshot_job.py \
  tests/test_build_market_quote_snapshots_cli.py \
  tests/test_market_quote_snapshot_tasks.py \
  tests/test_market_valuation_snapshot_model.py \
  tests/test_market_valuation_snapshots_repository.py \
  tests/test_market_valuation_snapshot_builder.py \
  tests/test_market_valuation_snapshot_job.py \
  tests/test_build_market_valuation_snapshots_cli.py \
  tests/test_market_valuation_snapshot_tasks.py \
  tests/test_invest_coverage_valuation.py \
  tests/test_invest_api_router_safety.py \
  -q

uv run --group dev ruff check \
  app/models/market_quote_snapshot.py \
  app/models/market_valuation_snapshot.py \
  app/services/market_quote_snapshots \
  app/services/market_valuation_snapshots \
  app/services/market_data_coverage \
  app/services/invest_coverage_service.py \
  app/jobs/market_quote_snapshots.py \
  app/jobs/market_valuation_snapshots.py \
  app/tasks/market_quote_snapshot_tasks.py \
  app/tasks/market_valuation_snapshot_tasks.py \
  scripts/build_market_quote_snapshots.py \
  scripts/build_market_valuation_snapshots.py \
  tests/test_invest_coverage_ohlcv.py \
  tests/test_market_quote_snapshot_*.py \
  tests/test_market_valuation_snapshot_*.py \
  tests/test_build_market_quote_snapshots_cli.py \
  tests/test_build_market_valuation_snapshots_cli.py
```

Type tooling (`ty`) on the new modules only if the branch already runs it. Do not block on unrelated repo-wide type debt.

### 6.6 Baseline before K1 changes

K1 must capture a baseline run on the rebased branch before writing code:

```bash
git rebase github/main   # see §2
uv run --group test pytest tests/test_invest_coverage.py -q
```

Result must already be all-passing on `github/main`. Record the pass count in the K1 handoff so post-change comparisons are meaningful.

## 7. Dry-run / approval packet procedure for K3/K4

After K1 lands and K2 reviews, the K3 deploy candidate must produce bounded dry-run packets **per surface, per market** before any `--commit` runs in production. Suggested commands (read-only):

```bash
# Quotes — KR, top 20 active symbols, dry-run
uv run python -m scripts.build_market_quote_snapshots --market kr --limit 20

# Quotes — US, top 20
uv run python -m scripts.build_market_quote_snapshots --market us --limit 20

# Quotes — crypto, top 20 (Upbit KRW)
uv run python -m scripts.build_market_quote_snapshots --market crypto --limit 20

# Valuation — KR, top 20
uv run python -m scripts.build_market_valuation_snapshots --market kr --limit 20

# Valuation — US, top 20
uv run python -m scripts.build_market_valuation_snapshots --market us --limit 20
```

Approval packet (per command) must include:

- Command run + git SHA + environment.
- `committed=false` confirmation from the result dataclass.
- Symbols resolved.
- Snapshots built.
- Idempotency split (wouldInsert / wouldUpdate / duplicatePayloadKeys).
- Warnings / skipped symbols.
- Sample rows (max 10, secret-redacted).
- Proposed `--commit` command + bounded scope + rollback note (DELETE FROM <table> WHERE collected_at >= <run_start_ts> is acceptable for first commits; longer-lived data should be ALTER'd, not deleted).

OHLCV does not need a new dry-run because `kr_candles_sync_service` / `us_candles_sync_service` already write durably; ROB-206 only **reads** them. K3/K4 should still re-run the existing `scripts/sync_kr_candles.py --mode incremental` and `scripts/sync_us_candles.py --mode incremental` packets if /invest coverage shows partial/stale OHLCV after deploy.

## 8. Approval gates / prohibited actions

Restate these gates in every K1/K2/K3/K4 handoff:

- **Production DB writes / backfills**: any `--commit` against production requires explicit operator approval after a bounded dry-run packet. K1 and K2 must not pass `--commit` to any builder in any environment without that approval.
- **Recurring scheduler activation**: attaching `schedule=[...]` to `build_market_quote_snapshots` / `build_market_valuation_snapshots` (or unpausing such a schedule in production) is a separate explicit operator approval. K1 must ship the task wrappers without schedules.
- **Broker / order / watch / order-intent / paper-trade side effects**: forbidden. New builders may **read** from `MarketDataClient.inquire_price`, `fetch_fast_info`, `naver_finance.fetch_*`, but must never call mutating endpoints. Tests must not network out to live brokers.
- **Request-path scraping**: `/invest/api/coverage` and `/invest/api/*` must remain read-only over durable tables. Builders are invoked only from the operator CLI / TaskIQ task, never from a FastAPI request handler.
- **Secrets / env printing**: redact `raw_payload` keys before persisting; never log full API keys; reuse existing `safe_log_value` / `_redact_sensitive_keys` helpers.
- **Crypto OHLCV / valuation expansion**: explicitly out of scope. Adding a `crypto_candles_*` hypertable or crypto valuation is a follow-up Linear ticket (suggested: ROB-206-followup-crypto-ohlcv).

## 9. Main risks and mitigations

1. **Migration head conflicts.** Multiple ROB-2xx PRs land in parallel; alembic head can drift.
   - Mitigation: rebase before generating migration, run `uv run alembic heads`, regenerate if needed. Use `merge_duplicate_heads` pattern from `f29d2ab2ca96` if two heads land separately.

2. **Large response payload from coverage endpoint.** Adding three more surfaces × three markets × candidates can grow the JSON.
   - Mitigation: keep `samples=10` cap in builders, do not embed samples in coverage response.

3. **`raw_payload` JSONB bloat.** Quote snapshots are intra-day → many rows × big JSONB can balloon.
   - Mitigation: cap `raw_payload` retained fields to a redacted whitelist; document retention follow-up. K1 should not enable retention policies yet (that is a separate ops/approval task).

4. **Yahoo / Naver request-time outages during backfill.** Builders may fail mid-symbol.
   - Mitigation: per-symbol try/except → warning row; commit only successful payloads; idempotency means re-running fills gaps.

5. **`provider_unwired` test expectations break.** Tests that assert `quotes` / `ohlcv` / `valuation_fundamentals` are `provider_unwired` (`tests/test_invest_coverage.py` lines 214-215) must be updated.
   - Mitigation: update the asserts to expect `missing` (post-migration, pre-backfill) or seed test rows for `fresh`. Plan calls this out explicitly so K1 doesn't get blindsided by red CI.

6. **Test DB does not have TimescaleDB.** OHLCV freshness queries hit `kr_candles_1m` which is created via raw SQL.
   - Mitigation: tests use existing fixture (see `tests/test_kr_candles_sync.py`) or wrap freshness query in a try/except that returns `missing` if the table does not exist (and log once). Prefer the fixture path so we exercise the real query.

7. **Branch tracking confusion.** This worktree's `origin` points at the local mirror, not GitHub. PRs must be pushed against `github` remote.
   - Mitigation: K1 sets upstream explicitly: `git push -u github feature/rob-206-durable-market-read-models`.

## 10. Suggested downstream task sequence

- **K1 (implementer, Sonnet preferred):** Execute Stages A → B → C in order on the rebased branch. One commit per stage is fine; one PR. Run §6 test/lint commands. Hand off pass counts, file diff list, and a checklist of files touched.
- **K2 (reviewer, Opus preferred):** Verify branch diff vs `github/main`; verify no `schedule=[...]`; verify migration head is unique; verify coverage tests now exercise `MarketQuoteSnapshot` / `MarketValuationSnapshot` / hypertables; verify no broker mutation imports added to `/invest` router graph; verify approval gates restated in PR description.
- **K3 (deploy/dry-run):** Open PR against `main` only, wait for normal review. After merge, deploy candidate generates per-market dry-run packets per §7. Post to Linear. Block on explicit production write approval.
- **K4 (ops, approval-gated):** Only after operator approval (named scope, expected rows, rollback): run `--commit` per market. After write completes, capture before/after coverage states from `/invest/coverage` and confirm `quotes` / `valuation_fundamentals` are `fresh` (or `partial`) for the chosen scope, and `ohlcv` is `fresh` for kr/us. Scheduler activation is a separate K4-extra approval.

## 11. Files this plan touches (canonical list)

New files:

- `app/models/market_quote_snapshot.py`
- `app/models/market_valuation_snapshot.py`
- `app/services/market_quote_snapshots/__init__.py`
- `app/services/market_quote_snapshots/repository.py`
- `app/services/market_quote_snapshots/freshness.py`
- `app/services/market_quote_snapshots/builder.py`
- `app/services/market_valuation_snapshots/__init__.py`
- `app/services/market_valuation_snapshots/repository.py`
- `app/services/market_valuation_snapshots/freshness.py`
- `app/services/market_valuation_snapshots/builder.py`
- `app/services/market_data_coverage/__init__.py`
- `app/services/market_data_coverage/ohlcv_freshness.py`
- `app/jobs/market_quote_snapshots.py`
- `app/jobs/market_valuation_snapshots.py`
- `app/tasks/market_quote_snapshot_tasks.py`
- `app/tasks/market_valuation_snapshot_tasks.py`
- `scripts/build_market_quote_snapshots.py`
- `scripts/build_market_valuation_snapshots.py`
- `alembic/versions/<hash>_add_market_quote_snapshots.py`
- `alembic/versions/<hash>_add_market_valuation_snapshots.py`
- `docs/runbooks/durable-market-read-models.md` (operator runbook; one short page)
- `tests/test_invest_coverage_ohlcv.py`
- `tests/test_market_quote_snapshot_model.py`
- `tests/test_market_quote_snapshots_repository.py`
- `tests/test_market_quote_snapshot_freshness.py`
- `tests/test_market_quote_snapshot_builder.py`
- `tests/test_market_quote_snapshot_job.py`
- `tests/test_build_market_quote_snapshots_cli.py`
- `tests/test_market_quote_snapshot_tasks.py`
- `tests/test_market_valuation_snapshot_model.py`
- `tests/test_market_valuation_snapshots_repository.py`
- `tests/test_market_valuation_snapshot_builder.py`
- `tests/test_market_valuation_snapshot_job.py`
- `tests/test_build_market_valuation_snapshots_cli.py`
- `tests/test_market_valuation_snapshot_tasks.py`
- `tests/test_invest_coverage_valuation.py`

Modified files:

- `app/models/__init__.py` (import new models for alembic autogenerate).
- `app/tasks/__init__.py` (import new task modules).
- `app/services/invest_coverage_service.py` (add `_ohlcv_surfaces`, `_quote_surfaces`, `_valuation_surfaces`; remove the three surfaces from `_provider_unwired_surfaces`; update `_SURFACE_QUEUE`).
- `tests/test_invest_coverage.py` (update existing `provider_unwired` assertions to the new states; keep static naver candidate assertions intact).
- (No change required to `app/schemas/invest_coverage.py`.)

No deletions.
