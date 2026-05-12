# ROB-208 Market Events Rolling Scheduler + Coverage Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the rolling-window ingestion scheduler for the existing `market_events` foundation (ROB-128) and add a read-only freshness/coverage diagnostics surface, gated by an explicit production-write feature flag. No DB schema changes. No broker / order / watch / order-intent side effects. Production scheduler activation remains behind an operator-approved env flag.

**Architecture:** Reuse the ROB-128 ingestion code unchanged. Add (1) a `MarketEventsFreshnessService` that aggregates over the existing `market_event_ingestion_partitions` and `market_events` tables, (2) a read-only `GET /trading/api/market-events/freshness` router endpoint, (3) a scheduler-agnostic job runner `run_market_events_rolling_window(...)` in `app/jobs/`, (4) thin TaskIQ cron wrappers in `app/tasks/market_events_tasks.py` that follow the existing `research_run_refresh_tasks.py` pattern, (5) a `MARKET_EVENTS_INGEST_COMMIT_ENABLED` settings flag (default `False`) so the scheduler runs in dry-run / partition-state-only mode in production until an operator flips the gate, and (6) a `--retry-failed` CLI option to retry partitions stuck in `failed` state. Schedules run daily on per-source windows. The scheduler does **not** call broker mutation, watch alerts, or order-intent code paths.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x async, PostgreSQL with JSONB, TaskIQ (Redis ListQueueBroker, LabelScheduleSource), Pydantic v2, pytest + pytest-asyncio.

**Model handoff note:** Per the Hermes task body, planner runs on Claude Code Opus, implementer runs on Claude Code Sonnet, reviewer/final on Opus. Record the actual model used at each step in the Kanban handoff metadata.

---

## Pre-conditions / Reference reading

Before starting Task 1, read:

- `app/models/market_events.py` (ORM models — already exist; no schema change in this plan).
- `app/services/market_events/repository.py` (sole writer; do not introduce alternate writers).
- `app/services/market_events/ingestion.py` (`ingest_us_earnings_for_date`, `ingest_kr_disclosures_for_date`, `ingest_economic_events_for_date`).
- `scripts/ingest_market_events.py` (CLI; we extend it).
- `docs/runbooks/market-events-ingestion.md` (runbook; we extend it).
- `app/tasks/research_run_refresh_tasks.py` (TaskIQ cron template).
- `app/jobs/research_run_refresh_runner.py` (scheduler-agnostic orchestrator template — read-only summaries, never raises on operational skip conditions).
- `app/core/taskiq_broker.py` and `app/core/scheduler.py` (broker + scheduler wiring; LabelScheduleSource auto-discovers `@broker.task(schedule=...)` decorated tasks).
- `app/services/trade_journal_coverage_service.py` + `app/schemas/trade_journal.py` (`JournalCoverageResponse` shape — model the freshness response on this pattern: `generated_at`, `rows`, `warnings`).

---

## File Structure

**Create:**
- `app/services/market_events/freshness_service.py` — `MarketEventsFreshnessService` (read-only aggregator)
- `app/schemas/market_events_freshness.py` — `MarketEventsFreshnessRow`, `MarketEventsFreshnessResponse`
- `app/jobs/market_events_rolling_window.py` — `run_market_events_rolling_window(...)` scheduler-agnostic orchestrator
- `app/tasks/market_events_tasks.py` — TaskIQ cron wrappers (one per source, dry-run by default)
- `tests/services/test_market_events_freshness_service.py`
- `tests/test_market_events_freshness_router.py`
- `tests/jobs/test_market_events_rolling_window.py`
- `tests/tasks/test_market_events_tasks.py`

**Modify:**
- `app/routers/market_events.py` — add `GET /trading/api/market-events/freshness`
- `app/core/config.py` — add 4 settings: `market_events_ingest_commit_enabled`, `market_events_rolling_window_days_back`, `market_events_rolling_window_days_forward`, `market_events_rolling_window_max_partitions_per_run`
- `scripts/ingest_market_events.py` — add `--retry-failed` flag that re-ingests only partitions with `status='failed'` inside the given range
- `tests/test_market_events_cli.py` — add tests for `--retry-failed`
- `docs/runbooks/market-events-ingestion.md` — append "Scheduler activation" and "Freshness diagnostics" sections; restate approval gates
- `CLAUDE.md` — add ROB-208 entry under "Market Events Ingestion Foundation (ROB-128)" referencing the new scheduler/freshness surfaces and the activation gate

**Do NOT modify:**
- `app/models/market_events.py` (no schema changes)
- `app/services/market_events/repository.py` (writes still go only through the existing repository)
- Any broker / order / watch / order-intent / scheduling-side-effect code (out of scope and explicit safety boundary)
- `production` branch checkout or `shared/current` (per worktree rules)

---

## Approval Gates (restated; ALL required for production activation)

These gates apply to the implementer (K1–K3) and the final handoff (K4). The planner (this task) does not exercise any of them.

1. **No production DB write in CI / local dev / staging unless** the implementer is running an explicitly approved one-off CLI invocation with `--commit` semantics (the scheduler itself is gated separately, below).
2. **`MARKET_EVENTS_INGEST_COMMIT_ENABLED` defaults to `False`** in `app/core/config.py`. When `False`, the TaskIQ scheduler task short-circuits before calling `ingest_*_for_date` and instead writes a `dry_run` marker row into the partition table (or no-ops, see Task 4 step 3 for the chosen behavior). This is the production write gate.
3. **Recurring scheduler activation requires separate approval** (operator must explicitly enable the cron schedule by flipping `MARKET_EVENTS_INGEST_COMMIT_ENABLED=true` on the deployed scheduler process). The label-discovery `@broker.task(schedule=...)` is registered in code so the task is *visible* to the scheduler immediately on deploy, but the task body checks the flag and skips when disabled. There is no compile-time toggle of the cron registration.
4. **Backfill window approvals must specify**: (source, category, market) tuple, exact `from_date`–`to_date` range, dry-run vs commit, and an operator token. A backfill is an explicit `scripts.ingest_market_events` CLI invocation by the operator — never an automated scheduler action.
5. **No broker / order / watch / order-intent / paper-trading mutation** is allowed anywhere in this issue. All freshness reads are read-only.
6. **Sensitive payload handling unchanged**: `raw_payload_json` columns must continue to pass through the existing `_redact_sensitive_keys` helper inside `MarketEventsRepository.upsert_event_with_values`. Do not bypass it. Do not log raw payloads from the new scheduler task.

Each implementer task below restates the relevant gate in its acceptance criteria.

---

## Data Model Decisions

No schema changes. We add no migrations. We rely on the existing tables:

- `market_events` (event rows, idempotent upserts)
- `market_event_values` (metric values)
- `market_event_ingestion_partitions` (one row per `(source, category, market, partition_date)`, status in `{pending, running, succeeded, failed, partial}` — `taxonomy.PARTITION_STATUSES`)

The freshness service derives all diagnostics by SELECTing these tables. The rolling-window job writes only through the existing `MarketEventsRepository` and `ingest_*_for_date` functions.

### Freshness response shape (modeled on `JournalCoverageResponse`)

```python
# app/schemas/market_events_freshness.py
from datetime import date, datetime
from pydantic import BaseModel, Field

class MarketEventsFreshnessRow(BaseModel):
    source: str                                  # finnhub | dart | forexfactory
    category: str                                # earnings | disclosure | economic
    market: str                                  # us | kr | global
    window_from: date                            # echo of the window the caller asked about
    window_to: date
    partition_count_total: int                   # partitions present in window
    partition_count_succeeded: int
    partition_count_failed: int
    partition_count_running: int
    partition_count_pending: int
    partition_count_missing: int                 # window days with NO row at all
    event_count_in_window: int                   # rows in market_events whose event_date in window
    latest_succeeded_partition_date: date | None
    latest_succeeded_finished_at: datetime | None
    hours_since_latest_succeeded: float | None
    latest_failed_partition_date: date | None
    latest_failed_error: str | None              # truncated to 500 chars
    expected_next_refresh_at: datetime | None    # informational; computed from cron
    stale: bool                                  # True if hours_since_latest_succeeded > stale_threshold_hours

class MarketEventsFreshnessResponse(BaseModel):
    generated_at: datetime
    window_from: date
    window_to: date
    stale_threshold_hours: float
    rows: list[MarketEventsFreshnessRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
```

`stale_threshold_hours` defaults to **30 hours** (one day plus a 6-hour grace). Tunable per source if Task 1 step 4 demands.

---

## Task 1: Freshness aggregator service + schemas

**Goal:** Build the read-only diagnostics that summarize ingestion partition health and event coverage in a window.

**Files:**
- Create: `app/schemas/market_events_freshness.py`
- Create: `app/services/market_events/freshness_service.py`
- Create: `tests/services/test_market_events_freshness_service.py`

**Acceptance:** Service is read-only. No writes, no broker calls, no holdings/watchlist joins (deferred). Returns one row per `(source, category, market)` tuple in `taxonomy.SOURCES × CATEGORIES × MARKETS` that has at least one partition or one event in the window. Stale window threshold default 30 hours; `expected_next_refresh_at` is `None` unless the caller passes a cron-derived hint.

### Step 1.1 — Pydantic schemas

- [ ] **Write the failing test** at `tests/services/test_market_events_freshness_service.py::test_freshness_response_schema_shape`:

```python
from datetime import UTC, date, datetime

import pytest

from app.schemas.market_events_freshness import (
    MarketEventsFreshnessResponse,
    MarketEventsFreshnessRow,
)


@pytest.mark.unit
def test_freshness_response_schema_shape() -> None:
    row = MarketEventsFreshnessRow(
        source="finnhub",
        category="earnings",
        market="us",
        window_from=date(2026, 5, 5),
        window_to=date(2026, 5, 12),
        partition_count_total=8,
        partition_count_succeeded=7,
        partition_count_failed=1,
        partition_count_running=0,
        partition_count_pending=0,
        partition_count_missing=0,
        event_count_in_window=120,
        latest_succeeded_partition_date=date(2026, 5, 11),
        latest_succeeded_finished_at=datetime(2026, 5, 12, 6, 0, tzinfo=UTC),
        hours_since_latest_succeeded=2.5,
        latest_failed_partition_date=date(2026, 5, 10),
        latest_failed_error="finnhub 429",
        expected_next_refresh_at=None,
        stale=False,
    )
    resp = MarketEventsFreshnessResponse(
        generated_at=datetime(2026, 5, 12, 8, 30, tzinfo=UTC),
        window_from=date(2026, 5, 5),
        window_to=date(2026, 5, 12),
        stale_threshold_hours=30.0,
        rows=[row],
        warnings=[],
    )
    assert resp.rows[0].source == "finnhub"
    assert resp.rows[0].latest_failed_error == "finnhub 429"
```

- [ ] **Run it to confirm it fails**: `uv run pytest tests/services/test_market_events_freshness_service.py::test_freshness_response_schema_shape -v` → ImportError.

- [ ] **Create the schema file** exactly matching the shape in "Data Model Decisions" above.

- [ ] **Re-run the test** and confirm it passes.

- [ ] **Commit** the schema-only change:
  ```bash
  git add app/schemas/market_events_freshness.py tests/services/test_market_events_freshness_service.py
  git commit -m "feat(market-events): add freshness response schema (ROB-208)"
  ```

### Step 1.2 — Service skeleton + integration test for empty window

- [ ] **Write failing test** `test_freshness_empty_window_returns_empty_rows`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_empty_window_returns_empty_rows(db_session) -> None:
    from datetime import date

    from app.services.market_events.freshness_service import (
        MarketEventsFreshnessService,
    )

    svc = MarketEventsFreshnessService(db_session)
    resp = await svc.compute(
        window_from=date(2099, 1, 1),
        window_to=date(2099, 1, 7),
    )
    assert resp.rows == []
    assert resp.window_from == date(2099, 1, 1)
    assert resp.window_to == date(2099, 1, 7)
    assert resp.stale_threshold_hours == 30.0
```

- [ ] **Run to confirm it fails**: `uv run pytest tests/services/test_market_events_freshness_service.py::test_freshness_empty_window_returns_empty_rows -v -m integration` → ImportError.

- [ ] **Create the service file** `app/services/market_events/freshness_service.py`:

```python
"""Read-only freshness aggregator for market_events partitions (ROB-208).

Never writes. Never raises on missing data. Returns a structured response with
one row per (source, category, market) tuple that has activity in the window.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventIngestionPartition
from app.schemas.market_events_freshness import (
    MarketEventsFreshnessResponse,
    MarketEventsFreshnessRow,
)

DEFAULT_STALE_THRESHOLD_HOURS = 30.0


@dataclass(frozen=True)
class _Key:
    source: str
    category: str
    market: str


class MarketEventsFreshnessService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def compute(
        self,
        *,
        window_from: date,
        window_to: date,
        stale_threshold_hours: float = DEFAULT_STALE_THRESHOLD_HOURS,
        expected_next_refresh_by_key: dict[tuple[str, str, str], datetime] | None = None,
        now: datetime | None = None,
    ) -> MarketEventsFreshnessResponse:
        if window_from > window_to:
            raise ValueError("window_from must be <= window_to")
        clock = now or datetime.now(UTC)

        partitions = await self._fetch_partitions(window_from, window_to)
        event_counts = await self._fetch_event_counts(window_from, window_to)

        keys: set[_Key] = {
            _Key(p.source, p.category, p.market) for p in partitions
        } | {_Key(s, c, m) for (s, c, m) in event_counts.keys()}

        rows = [
            self._row_for_key(
                key=k,
                partitions=[p for p in partitions if (p.source, p.category, p.market) == (k.source, k.category, k.market)],
                event_count=event_counts.get((k.source, k.category, k.market), 0),
                window_from=window_from,
                window_to=window_to,
                stale_threshold_hours=stale_threshold_hours,
                expected_next_refresh=(expected_next_refresh_by_key or {}).get(
                    (k.source, k.category, k.market)
                ),
                now=clock,
            )
            for k in sorted(keys, key=lambda x: (x.source, x.category, x.market))
        ]

        warnings: list[str] = []
        for r in rows:
            if r.partition_count_failed > 0:
                warnings.append(
                    f"{r.source}/{r.category}/{r.market}: {r.partition_count_failed} failed partition(s) in window"
                )
            if r.stale:
                warnings.append(
                    f"{r.source}/{r.category}/{r.market}: stale (>{stale_threshold_hours:.1f}h since last success)"
                )

        return MarketEventsFreshnessResponse(
            generated_at=clock,
            window_from=window_from,
            window_to=window_to,
            stale_threshold_hours=stale_threshold_hours,
            rows=rows,
            warnings=warnings,
        )

    async def _fetch_partitions(
        self, window_from: date, window_to: date
    ) -> list[MarketEventIngestionPartition]:
        stmt = select(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.partition_date >= window_from,
            MarketEventIngestionPartition.partition_date <= window_to,
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def _fetch_event_counts(
        self, window_from: date, window_to: date
    ) -> dict[tuple[str, str, str], int]:
        stmt = (
            select(
                MarketEvent.source,
                MarketEvent.category,
                MarketEvent.market,
                func.count(MarketEvent.id),
            )
            .where(
                MarketEvent.event_date >= window_from,
                MarketEvent.event_date <= window_to,
            )
            .group_by(MarketEvent.source, MarketEvent.category, MarketEvent.market)
        )
        out: dict[tuple[str, str, str], int] = {}
        for src, cat, mkt, cnt in (await self.db.execute(stmt)).all():
            out[(src, cat, mkt)] = int(cnt)
        return out

    def _row_for_key(
        self,
        *,
        key: _Key,
        partitions: Iterable[MarketEventIngestionPartition],
        event_count: int,
        window_from: date,
        window_to: date,
        stale_threshold_hours: float,
        expected_next_refresh: datetime | None,
        now: datetime,
    ) -> MarketEventsFreshnessRow:
        plist = list(partitions)
        by_status: dict[str, int] = defaultdict(int)
        for p in plist:
            by_status[p.status] += 1
        days_in_window = (window_to - window_from).days + 1
        partition_dates = {p.partition_date for p in plist}
        missing = max(0, days_in_window - len(partition_dates))

        succeeded = [p for p in plist if p.status == "succeeded"]
        latest_succeeded = max(
            succeeded, key=lambda p: (p.finished_at or datetime.min.replace(tzinfo=UTC)), default=None
        )
        hours_since = None
        if latest_succeeded is not None and latest_succeeded.finished_at is not None:
            delta = now - latest_succeeded.finished_at
            hours_since = round(delta.total_seconds() / 3600.0, 4)

        failed = [p for p in plist if p.status == "failed"]
        latest_failed = max(
            failed, key=lambda p: (p.partition_date), default=None
        )

        stale = (hours_since is None) or (hours_since > stale_threshold_hours)

        return MarketEventsFreshnessRow(
            source=key.source,
            category=key.category,
            market=key.market,
            window_from=window_from,
            window_to=window_to,
            partition_count_total=len(plist),
            partition_count_succeeded=by_status.get("succeeded", 0),
            partition_count_failed=by_status.get("failed", 0),
            partition_count_running=by_status.get("running", 0),
            partition_count_pending=by_status.get("pending", 0),
            partition_count_missing=missing,
            event_count_in_window=event_count,
            latest_succeeded_partition_date=(
                latest_succeeded.partition_date if latest_succeeded else None
            ),
            latest_succeeded_finished_at=(
                latest_succeeded.finished_at if latest_succeeded else None
            ),
            hours_since_latest_succeeded=hours_since,
            latest_failed_partition_date=(
                latest_failed.partition_date if latest_failed else None
            ),
            latest_failed_error=(
                (latest_failed.last_error[:500] if latest_failed and latest_failed.last_error else None)
            ),
            expected_next_refresh_at=expected_next_refresh,
            stale=stale,
        )
```

- [ ] **Re-run the test** to confirm it passes.

- [ ] **Commit**:
  ```bash
  git add app/services/market_events/freshness_service.py tests/services/test_market_events_freshness_service.py
  git commit -m "feat(market-events): add read-only freshness aggregator (ROB-208)"
  ```

### Step 1.3 — Service test with mixed partition states

- [ ] **Write failing test** `test_freshness_aggregates_mixed_partition_states` that seeds:
  - one succeeded partition for `(finnhub, earnings, us)` finished 2 hours ago,
  - one failed partition with `last_error="finnhub 429"` for the same key, on a different date,
  - one succeeded partition for `(dart, disclosure, kr)` finished 50 hours ago,
  - and verifies: `partition_count_succeeded`, `partition_count_failed`, `hours_since_latest_succeeded` (~2.0), `stale=False` for the finnhub row, `stale=True` for the dart row (50 > 30), `latest_failed_error="finnhub 429"`, and that `warnings` contains both a "failed partition(s)" entry and a "stale" entry.

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_aggregates_mixed_partition_states(db_session) -> None:
    from datetime import date, datetime, timedelta, UTC

    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events.freshness_service import (
        MarketEventsFreshnessService,
    )

    now = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)
    db_session.add_all([
        MarketEventIngestionPartition(
            source="finnhub", category="earnings", market="us",
            partition_date=date(2026, 5, 11),
            status="succeeded",
            event_count=42,
            finished_at=now - timedelta(hours=2),
        ),
        MarketEventIngestionPartition(
            source="finnhub", category="earnings", market="us",
            partition_date=date(2026, 5, 10),
            status="failed",
            event_count=0,
            last_error="finnhub 429",
        ),
        MarketEventIngestionPartition(
            source="dart", category="disclosure", market="kr",
            partition_date=date(2026, 5, 10),
            status="succeeded",
            event_count=15,
            finished_at=now - timedelta(hours=50),
        ),
    ])
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    resp = await svc.compute(
        window_from=date(2026, 5, 5),
        window_to=date(2026, 5, 12),
        now=now,
    )

    finnhub = next(r for r in resp.rows if r.source == "finnhub")
    dart = next(r for r in resp.rows if r.source == "dart")
    assert finnhub.partition_count_succeeded == 1
    assert finnhub.partition_count_failed == 1
    assert finnhub.hours_since_latest_succeeded == pytest.approx(2.0, abs=0.1)
    assert finnhub.stale is False
    assert finnhub.latest_failed_error == "finnhub 429"
    assert dart.stale is True
    assert any("failed partition" in w for w in resp.warnings)
    assert any("stale" in w for w in resp.warnings)
```

- [ ] **Run** and iterate the service until the test passes.

- [ ] **Commit** with message `test(market-events): freshness service handles mixed partition states (ROB-208)`.

### Step 1.4 — Decide per-source stale thresholds

- [ ] **Open question for the implementer**: by default we use a single 30h threshold. If during Task 1.3 it becomes obvious that ForexFactory's weekly XML should be allowed e.g. 168h before going stale, parameterize via `stale_threshold_hours_by_key: dict[tuple[str, str, str], float] | None = None` on `MarketEventsFreshnessService.compute(...)`. Otherwise leave the single threshold and document the choice in the runbook (Task 7).

---

## Task 2: Freshness router endpoint

**Files:**
- Modify: `app/routers/market_events.py`
- Create: `tests/test_market_events_freshness_router.py`

**Acceptance:** `GET /trading/api/market-events/freshness?from_date=&to_date=&stale_threshold_hours=` returns `MarketEventsFreshnessResponse`. Auth required (`get_authenticated_user` dependency, matching the existing endpoints in the same router). No mutation. Default window is `today-7..today+7` if both `from_date` and `to_date` are omitted; if only one is provided, error 400.

### Step 2.1 — Test for default window

- [ ] **Write failing test** `test_get_freshness_default_window_returns_200`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_freshness_default_window_returns_200(authed_client) -> None:
    resp = await authed_client.get("/trading/api/market-events/freshness")
    assert resp.status_code == 200
    data = resp.json()
    assert "generated_at" in data
    assert "window_from" in data
    assert "window_to" in data
    assert "stale_threshold_hours" in data
    assert isinstance(data["rows"], list)
    assert isinstance(data["warnings"], list)
```

(Use the existing `authed_client` fixture used by `tests/test_market_events_router.py` — copy the fixture pattern from there.)

- [ ] **Run** and confirm 404 / route-missing failure.

- [ ] **Add the endpoint** to `app/routers/market_events.py`:

```python
from datetime import timedelta

from app.schemas.market_events_freshness import MarketEventsFreshnessResponse
from app.services.market_events.freshness_service import (
    MarketEventsFreshnessService,
    DEFAULT_STALE_THRESHOLD_HOURS,
)


@router.get(
    "/api/market-events/freshness",
    response_model=MarketEventsFreshnessResponse,
)
async def get_market_events_freshness(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: date | None = None,
    to_date: date | None = None,
    stale_threshold_hours: float = DEFAULT_STALE_THRESHOLD_HOURS,
) -> MarketEventsFreshnessResponse:
    if (from_date is None) ^ (to_date is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_date and to_date must both be provided or both omitted",
        )
    today = date.today()
    window_from = from_date or (today - timedelta(days=7))
    window_to = to_date or (today + timedelta(days=7))
    svc = MarketEventsFreshnessService(db)
    try:
        return await svc.compute(
            window_from=window_from,
            window_to=window_to,
            stale_threshold_hours=stale_threshold_hours,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
```

- [ ] **Re-run test** and confirm it passes.

### Step 2.2 — Tests for partial windows and auth

- [ ] **Write failing test** `test_get_freshness_unauthenticated_returns_401`:
  ```python
  @pytest.mark.asyncio
  @pytest.mark.integration
  async def test_get_freshness_unauthenticated_returns_401(client) -> None:
      resp = await client.get("/trading/api/market-events/freshness")
      assert resp.status_code == 401
  ```
  (Use the non-authed `client` fixture — copy from `tests/test_market_events_router.py`.)

- [ ] **Write failing test** `test_get_freshness_partial_window_returns_400`:
  ```python
  @pytest.mark.asyncio
  @pytest.mark.integration
  async def test_get_freshness_partial_window_returns_400(authed_client) -> None:
      resp = await authed_client.get(
          "/trading/api/market-events/freshness?from_date=2026-05-05"
      )
      assert resp.status_code == 400
  ```

- [ ] **Run** all three router tests — they should now pass without further code changes.

- [ ] **Commit**:
  ```bash
  git add app/routers/market_events.py tests/test_market_events_freshness_router.py
  git commit -m "feat(market-events): add /freshness diagnostics endpoint (ROB-208)"
  ```

---

## Task 3: Production write gate config flag

**Files:**
- Modify: `app/core/config.py`
- Modify: `env.example` (add the new keys, default `false` / numeric defaults; commit only documentation defaults, not secrets)
- Create: `tests/test_market_events_config_flags.py`

**Acceptance:** Four new settings on the `Settings` class, all with **safe defaults** (scheduler stays disabled until an operator flips the flag):

| Setting | Type | Default | Purpose |
| --- | --- | --- | --- |
| `market_events_ingest_commit_enabled` | `bool` | `False` | Production write gate. When `False`, scheduler short-circuits before calling `ingest_*_for_date`. |
| `market_events_rolling_window_days_back` | `int` | `7` | Lookback days for daily rolling-window run. |
| `market_events_rolling_window_days_forward` | `int` | `60` | Forward window for sources that publish a calendar (finnhub, forexfactory). DART forces 0. |
| `market_events_rolling_window_max_partitions_per_run` | `int` | `90` | Safety cap. Refuse to ingest more than N partitions per run; warn and stop. |

### Step 3.1 — Settings test

- [ ] **Write failing test** `test_market_events_settings_have_safe_defaults`:

```python
@pytest.mark.unit
def test_market_events_settings_have_safe_defaults() -> None:
    from app.core.config import settings

    assert settings.market_events_ingest_commit_enabled is False
    assert settings.market_events_rolling_window_days_back == 7
    assert settings.market_events_rolling_window_days_forward == 60
    assert settings.market_events_rolling_window_max_partitions_per_run == 90
```

- [ ] **Run** and confirm failure (attribute missing).

- [ ] **Add the four fields** in the appropriate section of `app/core/config.py` (near the `research_run_refresh_*` block around line 257):

```python
# ROB-208 — market events rolling scheduler + activation gate
market_events_ingest_commit_enabled: bool = False
market_events_rolling_window_days_back: int = 7
market_events_rolling_window_days_forward: int = 60
market_events_rolling_window_max_partitions_per_run: int = 90
```

- [ ] **Re-run test** and confirm it passes.

- [ ] **Append the same keys to `env.example`** with inline comments stating that `market_events_ingest_commit_enabled=true` enables production writes (operator approval required). Do not commit any real credential value.

- [ ] **Commit**:
  ```bash
  git add app/core/config.py env.example tests/test_market_events_config_flags.py
  git commit -m "feat(market-events): add rolling-window scheduler config flags, default disabled (ROB-208)"
  ```

---

## Task 4: Rolling-window job runner (scheduler-agnostic)

**Files:**
- Create: `app/jobs/market_events_rolling_window.py`
- Create: `tests/jobs/test_market_events_rolling_window.py`

**Acceptance:** Pure orchestrator. Imports nothing from TaskIQ. Returns a structured summary dict (`TypedDict`) including `status` in `{"completed", "disabled", "skipped", "error"}`, mirroring `app/jobs/research_run_refresh_runner.py`. When `market_events_ingest_commit_enabled` is `False`, the runner short-circuits with `status="disabled"` and writes **no** partition rows. When `True`, it calls `ingest_*_for_date` for each day in the per-source window (max-partition cap respected). Each per-day failure is recorded by the existing partition mechanism — the runner does **not** swallow exceptions silently; it logs and continues to the next day.

### Step 4.1 — Disabled-flag test

- [ ] **Write failing test** `test_runner_disabled_when_commit_flag_off`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_runner_disabled_when_commit_flag_off(db_session, monkeypatch) -> None:
    from datetime import date

    from app.jobs.market_events_rolling_window import run_market_events_rolling_window

    monkeypatch.setattr(
        "app.jobs.market_events_rolling_window.settings.market_events_ingest_commit_enabled",
        False,
        raising=False,
    )

    result = await run_market_events_rolling_window(
        source="finnhub",
        category="earnings",
        market="us",
        today=date(2026, 5, 12),
        db_factory=lambda: _yield_db(db_session),  # define helper in test
    )
    assert result["status"] == "disabled"
    assert result["dry_run"] is True
    assert result["partitions_attempted"] == 0
```

- [ ] **Run** to confirm import failure.

- [ ] **Create `app/jobs/market_events_rolling_window.py`**:

```python
"""ROB-208 — rolling-window market events ingestion orchestrator.

Scheduler-agnostic: no TaskIQ imports here so this function can be wrapped by any
scheduler (TaskIQ cron tasks in app/tasks/market_events_tasks.py, or a future
Prefect @flow in a separate package).

Returns a structured summary dict; never raises on operational skip conditions
(commit flag disabled, empty window). Per-day fetch/normalize failures are
recorded by the partition mechanism inside ingest_*_for_date; the runner logs
and continues.

This module does NOT call broker / order / watch / order-intent code paths.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_us_earnings_for_date,
)

logger = logging.getLogger(__name__)

SourceLiteral = Literal["finnhub", "dart", "forexfactory"]
CategoryLiteral = Literal["earnings", "disclosure", "economic"]
MarketLiteral = Literal["us", "kr", "global"]
StatusLiteral = Literal["completed", "disabled", "skipped", "error"]


class RollingWindowSummary(TypedDict, total=False):
    status: StatusLiteral
    reason: str
    source: str
    category: str
    market: str
    window_from: str
    window_to: str
    dry_run: bool
    partitions_attempted: int
    partitions_succeeded: int
    partitions_failed: int
    events_upserted: int
    warnings: list[str]


_DISPATCH: dict[
    tuple[SourceLiteral, CategoryLiteral, MarketLiteral],
    Callable[[AsyncSession, date], Awaitable[object]],
] = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
    ("forexfactory", "economic", "global"): ingest_economic_events_for_date,
}


@asynccontextmanager
async def _default_db_factory():
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


def _window_for(
    *,
    source: SourceLiteral,
    today: date,
    days_back: int,
    days_forward: int,
) -> tuple[date, date]:
    if source == "dart":
        # DART is past-only (disclosures publish as they happen)
        return (today - timedelta(days=days_back), today)
    return (today - timedelta(days=days_back), today + timedelta(days=days_forward))


async def run_market_events_rolling_window(
    *,
    source: SourceLiteral,
    category: CategoryLiteral,
    market: MarketLiteral,
    today: date | None = None,
    db_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RollingWindowSummary:
    key = (source, category, market)
    if key not in _DISPATCH:
        return {
            "status": "error",
            "reason": f"unsupported source/category/market: {key}",
            "source": source, "category": category, "market": market,
            "partitions_attempted": 0, "partitions_succeeded": 0,
            "partitions_failed": 0, "events_upserted": 0, "warnings": [],
            "dry_run": True,
        }

    days_back = settings.market_events_rolling_window_days_back
    days_forward = settings.market_events_rolling_window_days_forward
    cap = settings.market_events_rolling_window_max_partitions_per_run
    commit = bool(settings.market_events_ingest_commit_enabled)

    target_today = today or date.today()
    window_from, window_to = _window_for(
        source=source, today=target_today, days_back=days_back, days_forward=days_forward
    )
    days = (window_to - window_from).days + 1

    base: RollingWindowSummary = {
        "source": source, "category": category, "market": market,
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "partitions_attempted": 0, "partitions_succeeded": 0,
        "partitions_failed": 0, "events_upserted": 0, "warnings": [],
        "dry_run": not commit,
    }

    if days > cap:
        return {
            **base,
            "status": "skipped",
            "reason": f"window {days} > max_partitions_per_run {cap}",
        }

    if not commit:
        logger.info(
            "market_events rolling window disabled (commit flag off): %s/%s/%s window=%s..%s days=%d",
            source, category, market, window_from, window_to, days,
        )
        return {
            **base,
            "status": "disabled",
            "reason": "market_events_ingest_commit_enabled=false",
        }

    fn = _DISPATCH[key]
    succeeded = failed = upserted = 0
    factory = db_factory or _default_db_factory

    cur = window_from
    while cur <= window_to:
        async with factory() as db:
            try:
                result = await fn(db, cur)
                await db.commit()
                if getattr(result, "status", None) == "succeeded":
                    succeeded += 1
                    upserted += int(getattr(result, "event_count", 0) or 0)
                else:
                    failed += 1
                    logger.warning(
                        "rolling-window partition not succeeded: %s/%s/%s on %s status=%s",
                        source, category, market, cur, getattr(result, "status", None),
                    )
            except Exception as exc:
                failed += 1
                logger.exception(
                    "rolling-window partition crashed: %s/%s/%s on %s", source, category, market, cur
                )
        cur += timedelta(days=1)

    return {
        **base,
        "status": "completed",
        "partitions_attempted": days,
        "partitions_succeeded": succeeded,
        "partitions_failed": failed,
        "events_upserted": upserted,
    }
```

- [ ] **Re-run** the test from Step 4.1 and confirm it passes.

### Step 4.2 — Enabled-flag dry-run test (no live API calls)

- [ ] **Write failing test** `test_runner_enabled_dispatches_per_day_and_records_results`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_runner_enabled_dispatches_per_day_and_records_results(db_session, monkeypatch) -> None:
    from datetime import date

    from app.jobs import market_events_rolling_window as runner

    monkeypatch.setattr(
        "app.jobs.market_events_rolling_window.settings.market_events_ingest_commit_enabled",
        True, raising=False,
    )
    monkeypatch.setattr(
        "app.jobs.market_events_rolling_window.settings.market_events_rolling_window_days_back",
        2, raising=False,
    )
    monkeypatch.setattr(
        "app.jobs.market_events_rolling_window.settings.market_events_rolling_window_days_forward",
        0, raising=False,
    )

    async def fake_ingest(db, target):
        class R:
            status = "succeeded"
            event_count = 5
        return R()

    monkeypatch.setitem(
        runner._DISPATCH,
        ("finnhub", "earnings", "us"),
        fake_ingest,
    )

    result = await runner.run_market_events_rolling_window(
        source="finnhub", category="earnings", market="us",
        today=date(2026, 5, 12),
        db_factory=lambda: _yield_db(db_session),
    )
    assert result["status"] == "completed"
    assert result["partitions_attempted"] == 3  # today-2, today-1, today
    assert result["partitions_succeeded"] == 3
    assert result["events_upserted"] == 15
    assert result["dry_run"] is False
```

- [ ] **Confirm the test passes** (the runner code already supports this).

- [ ] **Add a third test** for the cap: when `days > cap`, return `status="skipped"` and do not call the dispatch fn.

- [ ] **Add a fourth test** for DART specifically: with `days_back=3, days_forward=999`, DART must still produce window `today-3..today` (no forward days for DART).

- [ ] **Commit**:
  ```bash
  git add app/jobs/market_events_rolling_window.py tests/jobs/test_market_events_rolling_window.py
  git commit -m "feat(market-events): rolling-window orchestrator with commit-gated dispatch (ROB-208)"
  ```

---

## Task 5: TaskIQ cron wrappers

**Files:**
- Create: `app/tasks/market_events_tasks.py`
- Create: `tests/tasks/test_market_events_tasks.py`

**Acceptance:** Three thin TaskIQ-decorated wrappers (one per supported source), following exactly the `app/tasks/research_run_refresh_tasks.py` pattern. Each calls `run_market_events_rolling_window(...)` and returns its summary. The TaskIQ label-based scheduler (`LabelScheduleSource`) auto-discovers them; no further wiring needed beyond importing them once. The tasks themselves do not check the commit flag — the runner does. Tests verify the cron expressions, task names, and that calling the body returns the runner's summary.

### Step 5.1 — Schedule definitions

Recommended schedules (justified in the runbook update in Task 7):

| Source | Cron (UTC) | Cron in `Asia/Seoul` | Rationale |
| --- | --- | --- | --- |
| `finnhub / earnings / us` | `30 7 * * *` | — | Daily 07:30 UTC = before US preopen prep cycles. |
| `dart / disclosure / kr` | — | `0 6 * * *` KST | Daily 06:00 KST = after the previous KR session's late filings. |
| `forexfactory / economic / global` | `0 6 * * 0` | — | Weekly Sunday 06:00 UTC = after the weekly XML drops; rolling window catches mid-week revisions on the daily DART tick if needed. |

Choose UTC offset per source to avoid stacking three runs at the same minute.

### Step 5.2 — Write the wrappers

- [ ] **Write failing test** `test_market_events_tasks_have_expected_names_and_schedules`:

```python
@pytest.mark.unit
def test_market_events_tasks_have_expected_names_and_schedules() -> None:
    from app.tasks import market_events_tasks as t

    expected = {
        "market_events.finnhub_earnings_us_rolling",
        "market_events.dart_disclosure_kr_rolling",
        "market_events.forexfactory_economic_global_rolling",
    }
    fn_names = {
        getattr(fn, "task_name", None)
        for fn in (
            t.finnhub_earnings_us_rolling,
            t.dart_disclosure_kr_rolling,
            t.forexfactory_economic_global_rolling,
        )
    }
    assert fn_names == expected
```

(If `task_name` is not attached as an attribute by `@broker.task`, adjust the test to assert via the broker's task registry; mirror the pattern in `tests/test_research_run_refresh_tasks.py` if one exists.)

- [ ] **Create `app/tasks/market_events_tasks.py`**:

```python
"""ROB-208 — TaskIQ cron tasks for the market events rolling-window ingestion.

Each task is a thin wrapper around run_market_events_rolling_window(); the
runner is the single place that checks the production-write gate
(MARKET_EVENTS_INGEST_COMMIT_ENABLED). Disabling the gate makes every
scheduled run a structured no-op with status="disabled".

No broker / order / watch / order-intent side effects in this module.
"""

from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.market_events_rolling_window import (
    RollingWindowSummary,
    run_market_events_rolling_window,
)

logger = logging.getLogger(__name__)

_KST = "Asia/Seoul"


@broker.task(
    task_name="market_events.finnhub_earnings_us_rolling",
    schedule=[{"cron": "30 7 * * *", "cron_offset": "UTC"}],
)
async def finnhub_earnings_us_rolling() -> RollingWindowSummary:
    return await run_market_events_rolling_window(
        source="finnhub", category="earnings", market="us"
    )


@broker.task(
    task_name="market_events.dart_disclosure_kr_rolling",
    schedule=[{"cron": "0 6 * * *", "cron_offset": _KST}],
)
async def dart_disclosure_kr_rolling() -> RollingWindowSummary:
    return await run_market_events_rolling_window(
        source="dart", category="disclosure", market="kr"
    )


@broker.task(
    task_name="market_events.forexfactory_economic_global_rolling",
    schedule=[{"cron": "0 6 * * 0", "cron_offset": "UTC"}],
)
async def forexfactory_economic_global_rolling() -> RollingWindowSummary:
    return await run_market_events_rolling_window(
        source="forexfactory", category="economic", market="global"
    )
```

- [ ] **Re-run the test** and adjust the assertion shape until it passes.

### Step 5.3 — Task body smoke test (mocked runner)

- [ ] **Write integration-marked test** `test_finnhub_task_calls_runner` that monkeypatches `run_market_events_rolling_window` and asserts the task body returns its return value.

- [ ] **Verify the import side-effect**: confirm in a separate test (`test_market_events_tasks_registered_with_broker`) that importing the module is enough to register the tasks with `app.core.taskiq_broker.broker` — pattern: assert `"market_events.finnhub_earnings_us_rolling"` shows up in `broker.find_task(...)` (mirror whatever assertion is used in the codebase's existing scheduler-registration tests; if none exists, just import-and-skip-with-comment).

- [ ] **Commit**:
  ```bash
  git add app/tasks/market_events_tasks.py tests/tasks/test_market_events_tasks.py
  git commit -m "feat(market-events): add TaskIQ cron wrappers for rolling ingest (ROB-208)"
  ```

### Step 5.4 — Wire registration on startup

- [ ] **Verify** that the new task module is auto-discovered. TaskIQ workers in this repo typically import tasks at process startup. Check `app/main.py` or `taskiq` worker entrypoint. If task auto-discovery is import-driven, add a single import line:

```python
# in the worker bootstrap (app/main.py or app/core/taskiq_broker.py — match the existing pattern used by research_run_refresh_tasks)
from app.tasks import market_events_tasks  # noqa: F401  # ROB-208 — register cron tasks
```

If the pattern in `research_run_refresh_tasks.py` already auto-loads sibling modules, the new file will register automatically — verify by inspection and explicitly note "no further wiring needed" in the implementer's commit message.

- [ ] **Commit** the wiring change (if any) separately:
  ```bash
  git commit -m "chore(market-events): ensure scheduled tasks are imported on startup (ROB-208)"
  ```

---

## Task 6: CLI `--retry-failed` flag for partition cleanup

**Files:**
- Modify: `scripts/ingest_market_events.py`
- Modify: `tests/test_market_events_cli.py`

**Acceptance:** New `--retry-failed` boolean flag. When set, the CLI selects `market_event_ingestion_partitions` rows with `status="failed"` in `[from_date, to_date]` for the given `(source, category, market)`, and re-runs the per-day orchestrator only for those days. Without the flag, the existing behaviour is unchanged. With `--retry-failed` and `--dry-run` together: print the planned retry list, do not call ingest functions.

### Step 6.1 — Failing test

- [ ] **Write failing test** `test_run_ingest_retry_failed_only_retries_failed_partitions`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_retry_failed_only_retries_failed_partitions(
    db_session, monkeypatch
) -> None:
    from datetime import date
    from unittest.mock import AsyncMock

    from app.models.market_events import MarketEventIngestionPartition
    from scripts import ingest_market_events as cli

    db_session.add_all([
        MarketEventIngestionPartition(
            source="finnhub", category="earnings", market="us",
            partition_date=date(2026, 5, 7), status="succeeded", event_count=10,
        ),
        MarketEventIngestionPartition(
            source="finnhub", category="earnings", market="us",
            partition_date=date(2026, 5, 8), status="failed", event_count=0,
            last_error="429",
        ),
        MarketEventIngestionPartition(
            source="finnhub", category="earnings", market="us",
            partition_date=date(2026, 5, 9), status="failed", event_count=0,
            last_error="timeout",
        ),
    ])
    await db_session.flush()

    fake = AsyncMock(return_value=type("R", (), {"status": "succeeded", "event_count": 1})())
    monkeypatch.setitem(cli.SUPPORTED, ("finnhub", "earnings", "us"), fake)

    rc = await cli.run_ingest(
        db=db_session,
        source="finnhub", category="earnings", market="us",
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 9),
        dry_run=False,
        retry_failed=True,
    )

    assert rc == 0
    # Only the two failed partitions retried, not the succeeded one
    assert fake.await_count == 2
    call_dates = sorted(call.args[1] for call in fake.await_args_list)
    assert call_dates == [date(2026, 5, 8), date(2026, 5, 9)]
```

- [ ] **Run** and confirm the test fails (`run_ingest` does not accept `retry_failed`).

### Step 6.2 — Add the flag

- [ ] **Extend the CLI argparse** in `parse_args(...)`:

```python
parser.add_argument(
    "--retry-failed",
    action="store_true",
    dest="retry_failed",
    help="Only re-ingest partitions currently in status='failed' within the range.",
)
```

- [ ] **Extend `run_ingest(...)`** to accept `retry_failed: bool = False`. When True, before iterating partition dates, query `MarketEventIngestionPartition` filtered by `(source, category, market, status='failed', partition_date BETWEEN from_date AND to_date)` and iterate only those dates. Preserve dry-run semantics.

- [ ] **Wire the argument through `main(...)`**.

- [ ] **Re-run** the test and confirm it passes.

### Step 6.3 — Dry-run + retry-failed test

- [ ] **Write failing test** `test_run_ingest_retry_failed_dry_run_does_not_call_orchestrator` mirroring the existing dry-run test (line 133 of `tests/test_market_events_cli.py`). Confirm passes.

- [ ] **Commit**:
  ```bash
  git add scripts/ingest_market_events.py tests/test_market_events_cli.py
  git commit -m "feat(market-events): CLI --retry-failed flag for stale-partition cleanup (ROB-208)"
  ```

---

## Task 7: Runbook + CLAUDE.md updates

**Files:**
- Modify: `docs/runbooks/market-events-ingestion.md`
- Modify: `CLAUDE.md`

**Acceptance:** The runbook explains how to activate the scheduler safely. CLAUDE.md gains a one-paragraph entry under the existing ROB-128 section pointing at the new files and the activation gate.

### Step 7.1 — Append sections to the runbook

- [ ] **Add a "Scheduler (ROB-208)" section** under the existing "Recommended rolling window" note that:
  - Lists the three TaskIQ task names and cron schedules
  - States that `MARKET_EVENTS_INGEST_COMMIT_ENABLED=false` (default) makes all scheduled runs structured no-ops with `status="disabled"`
  - Documents the per-source rolling window (DART past-only; finnhub + forexfactory past+forward)
  - Restates the approval-gate checklist for production activation: (a) operator approval recorded in Kanban, (b) confirm DB row counts before, (c) flip flag, (d) wait for first cron tick, (e) verify with the new freshness endpoint, (f) leave flag on only as long as approval covers, otherwise disable.

- [ ] **Add a "Freshness diagnostics (ROB-208)" section** that documents:
  - `GET /trading/api/market-events/freshness?from_date=&to_date=&stale_threshold_hours=`
  - Default window `today-7..today+7`
  - Sample response showing one source row
  - Explanation of `partition_count_missing` (window days with no row at all — typically future days never attempted yet) vs `partition_count_failed` (rows in `failed` state).

- [ ] **Add a "Partition cleanup (ROB-208)" section** that documents `scripts/ingest_market_events.py --retry-failed` with an example invocation.

- [ ] **Update the "Follow-ups" list** to mark "Prefect deployment" as superseded by the TaskIQ scheduler in this issue, and to remove the rolling-window deployment item.

### Step 7.2 — CLAUDE.md entry

- [ ] **Append to the existing "Market Events Ingestion Foundation (ROB-128)" section** of `CLAUDE.md`:

```markdown
### Market Events Rolling Scheduler + Freshness (ROB-208)

`market_events` 의 일일 롤링 윈도우 인제스천 스케줄러 및 freshness/coverage 진단 surface.

- **Job runner**: `app/jobs/market_events_rolling_window.run_market_events_rolling_window` — 스케줄러 비종속 오케스트레이터. `MARKET_EVENTS_INGEST_COMMIT_ENABLED=false` 일 때 구조적 no-op (`status="disabled"`).
- **TaskIQ tasks**: `app/tasks/market_events_tasks.py` — finnhub/dart/forexfactory 각각 일/주별 cron. 자동 발견.
- **Freshness API**: `GET /trading/api/market-events/freshness` (read-only)
- **CLI cleanup**: `scripts/ingest_market_events.py --retry-failed` (status='failed' 파티션만 재시도)
- **런북**: `docs/runbooks/market-events-ingestion.md` 의 "Scheduler" / "Freshness diagnostics" 섹션

**안전 경계**: 프로덕션 쓰기는 `MARKET_EVENTS_INGEST_COMMIT_ENABLED=true` operator 승인 필요. broker/order/watch/order-intent mutation 없음. `raw_payload_json` 은 기존 `_redact_sensitive_keys` 경유.
```

- [ ] **Commit** docs together:
  ```bash
  git add docs/runbooks/market-events-ingestion.md CLAUDE.md
  git commit -m "docs(market-events): scheduler activation + freshness runbook (ROB-208)"
  ```

---

## Task 8: Verification + handoff

**Files:** No code changes. Run targeted tests + lint, then post results.

### Step 8.1 — Targeted test sweep

- [ ] **Run unit tests**:
  ```bash
  uv run pytest \
    tests/services/test_market_events_freshness_service.py \
    tests/test_market_events_freshness_router.py \
    tests/jobs/test_market_events_rolling_window.py \
    tests/tasks/test_market_events_tasks.py \
    tests/test_market_events_cli.py \
    tests/test_market_events_config_flags.py \
    -v
  ```
- [ ] **Run lint + format**:
  ```bash
  uv run ruff check .
  uv run ruff format --check .
  ```
- [ ] **Run type check**:
  ```bash
  make typecheck
  ```
- [ ] **Run smoke regression** on the pre-existing market_events suites to confirm no breakage:
  ```bash
  uv run pytest \
    tests/services/test_market_events_models.py \
    tests/services/test_market_events_taxonomy.py \
    tests/services/test_market_events_schemas.py \
    tests/services/test_market_events_normalizers.py \
    tests/services/test_market_events_repository.py \
    tests/services/test_market_events_ingestion.py \
    tests/services/test_market_events_query_service.py \
    tests/test_market_events_router.py \
    tests/test_market_events_cli.py \
    -v
  ```

### Step 8.2 — Final handoff packet (K4 — post-merge, after operator approval window opens)

Final handoff (K4) is **not** part of K1–K3 implementation. The K4 worker will:

1. Open the PR with the K1–K3 commits.
2. After CI passes, post a Linear comment with:
   - PR URL
   - Files changed (high-level summary)
   - Test results (pasted output of the Task 8.1 commands)
   - Confirmation that `MARKET_EVENTS_INGEST_COMMIT_ENABLED` defaults to `false` and was **not** changed by this PR
   - Confirmation that no broker/order/watch/order-intent code paths were touched
   - Confirmation that `raw_payload_json` redaction still goes through `_redact_sensitive_keys`
3. Merge to `main` only after review approval.
4. Wait for production deploy.
5. Run the production smoke from a deployed runner:
   - `GET /trading/api/market-events/freshness` — expect 401 unauthenticated (auth required)
   - `GET /healthz` — expect 200
   - Verify `market_events_ingest_commit_enabled` reads `false` in the deployed config (e.g. via an admin-only diagnostics page, or by confirming the flag is unset in the deploy secrets).
   - Read-only DB check: confirm the three new task labels are registered with the scheduler.
6. Post a Linear comment with the **approval-gated packet** restating that recurring scheduler activation requires:
   - Approve a specific operator + activation window
   - Set `MARKET_EVENTS_INGEST_COMMIT_ENABLED=true` only for that window
   - Verify at least one full daily cron tick succeeds
   - Re-check `/trading/api/market-events/freshness` and confirm `stale=false` and `partition_count_failed=0` for each source
   - Disable / unset the flag once the approved window closes (or convert to permanent on a separate ticket)

---

## Implementer guidance / non-goals

- **No DB schema changes.** Reuse existing tables.
- **No new sources.** Only the three sources already wired in ROB-128 / ROB-132 (finnhub, dart, forexfactory).
- **No held/watched join.** That remains a ROB-128 follow-up.
- **No frontend work.** `/invest/coverage` UI is out of scope; the freshness endpoint is operator-facing for now.
- **No live API calls in tests.** Mock all `ingest_*_for_date` dispatches inside the rolling-window tests; mock all upstream HTTP inside the runner test path.
- **Keep functions ≤ 80 lines and files focused.** Split if a file exceeds ~250 lines.
- **Commit often.** Each task above has explicit commit boundaries.
- **TDD throughout.** Tests first, smallest passing implementation, refactor, commit.

---

## Self-Review

Spec coverage:

- ✅ Rolling scheduler implementation → Tasks 4, 5
- ✅ Calendar coverage cleanup (failed partition retry) → Task 6
- ✅ Freshness diagnostics endpoint → Tasks 1, 2
- ✅ Production write gate / activation approval gate → Task 3 + restated approval gates above
- ✅ Runbook + CLAUDE.md updates → Task 7
- ✅ Verification + handoff with approval-gated packet → Task 8
- ✅ No broker/order/watch/order-intent side effects → guarded in every task acceptance line
- ✅ `raw_payload_json` redaction preserved → not modified (writes still go through `MarketEventsRepository`)
- ✅ Worktree-only changes → no `production` or `shared/current` edits
- ✅ Targeted backend tests with pasted evidence → Task 8.1

Placeholder scan: none.

Type consistency: `RollingWindowSummary` defined in Task 4 used in Task 5 wrappers; `MarketEventsFreshnessResponse` / `MarketEventsFreshnessRow` defined in Task 1 used in Task 2 router signature; settings keys defined in Task 3 read in Task 4 runner and surfaced in Task 7 runbook. Matching names across tasks.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-12-rob-208-market-events-scheduler-and-freshness.md`. The K0 deliverable is the plan itself; K1–K3 implementer tasks will execute it.**

Recommended execution path for downstream Kanban tasks:

- **K1 (implementer, Sonnet)**: Tasks 1–3 (freshness service + endpoint + config flag). Single commit batch with green CI.
- **K2 (implementer, Sonnet)**: Tasks 4–5 (rolling-window runner + TaskIQ wrappers). Single commit batch with green CI.
- **K3 (implementer, Sonnet)**: Tasks 6–7 (CLI cleanup + docs). Single commit batch with green CI. Open the PR at end of K3.
- **K4 (final handoff, Opus)**: Task 8. Post PR/CI/deploy/smoke evidence to Linear, restate approval gates, do **not** flip `MARKET_EVENTS_INGEST_COMMIT_ENABLED`.

If the K1 implementer discovers that the recommended cron times conflict with an existing scheduled task already pinned to the same minute, adjust ±5 minutes and document the change in the runbook update (Task 7).
