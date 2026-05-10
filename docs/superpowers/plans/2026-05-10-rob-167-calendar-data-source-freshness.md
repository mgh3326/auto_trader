# ROB-167 — `/invest/calendar` Source Coverage + Freshness Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only freshness/coverage diagnostics to `/invest/calendar` so empty months can be classified (no data vs. ingestion failure vs. not-yet-ingested vs. stale) and document the source-coverage gap matrix with concrete follow-up options.

**Architecture:** A new `MarketEventsFreshnessService` reads `market_event_ingestion_partitions` (already populated by the ROB-128 ingestion pipeline) and is exposed through (a) a new read-only endpoint `GET /trading/api/market-events/coverage` and (b) an enriched `CalendarMeta` returned by `GET /invest/api/calendar`. `build_calendar` adds a per-day `dataState` derived from the expected source/category/market matrix for that date. A read-only CLI (`scripts/diagnose_calendar_coverage.py`) prints the same matrix from the local DB. A coverage gap document (`docs/runbooks/calendar-source-coverage.md`) records what we ingest today, what is missing (KR holidays, dividends, IPO/subscription, KR earnings schedules, crypto major events), and recommended source/license/safety follow-ups.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 async / Pydantic v2 / pytest / Vite + React 18 / TypeScript / Vitest. Same stack as the existing `app/services/market_events/*` and `frontend/invest/*` code paths. No new runtime dependencies.

---

## Context anchors (read these once before Task 1)

- ROB-167 Linear: https://linear.app/mgh3326/issue/ROB-167
- ROB-128 runbook: `docs/runbooks/market-events-ingestion.md`
- Existing ingestion + tables: `app/services/market_events/{ingestion,repository,query_service,normalizers,taxonomy}.py`, `app/models/market_events.py`
- Existing calendar assembler: `app/services/invest_view_model/calendar_service.py:44` (`build_calendar`)
- Existing read API: `app/routers/market_events.py:29-111`, `app/routers/invest_api.py:119-138`
- Frontend: `frontend/invest/src/{api/calendar.ts,types/calendar.ts,pages/desktop/DesktopCalendarPage.tsx,pages/mobile/MobileCalendarPage.tsx,components/calendar/vm.ts}`

## Worktree + branch

- Worktree path: `/Users/mgh3326/worktrees/auto_trader/ROB-167-calendar-data-freshness` (already exists)
- Branch: `kanban/ROB-167-calendar-data-freshness` (already exists; 17 commits behind `origin/main` at plan time — Task 0 fast-forwards)
- DO NOT edit `/Users/mgh3326/work/auto_trader` directly.

## Safety boundaries (binding)

- Allowed: read-only DB queries, schema additions, new GET endpoints, frontend display changes, local + CI tests, PR/CI flows, post-merge production deploy, read-only production smoke.
- **Forbidden without separate explicit approval:** any DB UPDATE/DELETE/INSERT outside Alembic schema migrations, scheduler changes (Prefect deployments, cron, systemd timers), enabling recurring market-event ingestion, large production backfill, broker/order/watch/order-intent/live/paper execution.
- Plan does **not** add an Alembic migration — `market_event_ingestion_partitions` already exists from ROB-128.
- Plan does **not** call live source APIs (Finnhub/DART/ForexFactory). All freshness checks are SELECT-only against the DB.

## File structure (decomposition lock-in)

**New files (9):**

| Path | Responsibility |
| --- | --- |
| `app/schemas/calendar_freshness.py` | Pydantic models: `CalendarSourceStatus`, `CalendarCoverage`, `CalendarDayState` literal, `CoverageMatrixResponse`. |
| `app/services/market_events/freshness_service.py` | Read-only freshness queries (`MarketEventsFreshnessService.get_coverage_matrix(...)`, `get_per_day_states(...)`). Reads `market_event_ingestion_partitions` only. |
| `app/services/market_events/expected_sources.py` | Pure module enumerating which `(source, category, market)` triples are expected for a given date (e.g. weekdays, KR/US holidays). Single source of truth for "expected" coverage. |
| `scripts/diagnose_calendar_coverage.py` | Read-only CLI: prints per-day partition matrix + per-source freshness for `[from_date, to_date]`. No DB writes. |
| `docs/runbooks/calendar-source-coverage.md` | Source/coverage gap matrix + recommended follow-up sources (KR holidays, dividends, IPO, KR earnings schedules, crypto majors). |
| `tests/services/test_market_events_freshness_service.py` | Unit tests for freshness service (DB-backed). |
| `tests/services/test_market_events_expected_sources.py` | Unit tests for the expected-source enumeration (pure function, no DB). |
| `tests/test_market_events_coverage_router.py` | Endpoint test for `GET /trading/api/market-events/coverage`. |
| `tests/test_diagnose_calendar_coverage_cli.py` | CLI smoke test for the diagnose script. |
| `frontend/invest/src/__tests__/calendarFreshnessVm.test.ts` | Frontend VM helpers for source-status badge labels. |

**Modified files (8):**

| Path | What changes |
| --- | --- |
| `app/schemas/invest_calendar.py` | Extend `CalendarMeta` with `sourceFreshness: list[CalendarSourceStatus]` + `coverage: CalendarCoverage`. Add `dataState` field on `CalendarDay`. |
| `app/services/invest_view_model/calendar_service.py` | Inject `MarketEventsFreshnessService`, populate new meta fields + per-day `dataState`. |
| `app/routers/market_events.py` | Add `GET /trading/api/market-events/coverage`. |
| `app/routers/invest_api.py` | No signature change to `/invest/api/calendar` — just consume new schema. |
| `frontend/invest/src/types/calendar.ts` | Add `CalendarSourceStatus`, `CalendarCoverage`, `CalendarDayState`, extend `CalendarDay` + `CalendarMeta`. |
| `frontend/invest/src/components/calendar/vm.ts` | Add `freshnessBadgeLabel(state)` + `dataStateLabel(state)` helpers. |
| `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx` | Render a freshness indicator row above the events grid (read-only, defaults hidden when all sources fresh). |
| `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx` | Render the same freshness indicator (compact). |
| `docs/runbooks/market-events-ingestion.md` | Add a "Calendar source coverage gaps" subsection that links to the new doc. |

**Touched but not modified (assertions only in tests):** `tests/test_invest_calendar_router.py` gets two added test cases.

---

## Task 0: Refresh worktree + capture baseline

**Files:** none changed (read-only setup)

- [ ] **Step 1: Fast-forward the kanban branch from `origin/main`**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-167-calendar-data-freshness
git fetch origin
git status
git merge --ff-only origin/main
git log --oneline -5
```

Expected: `HEAD` now points at `c7e3fb9b` (or a newer `origin/main` tip) and includes ROB-165 (`17517132`) + ROB-166 (`8ab37c3c`).

If `--ff-only` fails because the branch has diverged: stop and report blocked — the planner expected a clean fast-forward.

- [ ] **Step 2: Read-only DB sanity check (skip if local DB is empty)**

```bash
uv run python - <<'PY'
import asyncio
from sqlalchemy import select, func
from app.core.db import AsyncSessionLocal
from app.models.market_events import MarketEvent, MarketEventIngestionPartition

async def main():
    async with AsyncSessionLocal() as db:
        events = (await db.execute(select(func.count()).select_from(MarketEvent))).scalar_one()
        partitions = (await db.execute(select(func.count()).select_from(MarketEventIngestionPartition))).scalar_one()
        print({"market_events": events, "partitions": partitions})

asyncio.run(main())
PY
```

Expected: prints a JSON-ish dict with row counts. If both are 0, the diagnostic CLI in Task 8 will still produce meaningful "no data ingested" output. If the local DB is unreachable (no Postgres up), proceed — all tests use injected sessions/fixtures.

- [ ] **Step 3: No commit yet** (this task makes no source changes).

---

## Task 1: Add `expected_sources` pure module + tests

**Files:**
- Create: `app/services/market_events/expected_sources.py`
- Create: `tests/services/test_market_events_expected_sources.py`

**Why first:** the freshness service in Task 2 depends on knowing which `(source, category, market)` triples *should* exist for a given date, so we can distinguish "no data ingested" from "no data exists." Implemented as a pure function (no DB) so it's trivially testable and can be reused by the diagnostic CLI.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_market_events_expected_sources.py
"""Unit tests for app.services.market_events.expected_sources."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)


@pytest.mark.unit
def test_expected_sources_includes_finnhub_dart_forexfactory_on_weekday() -> None:
    triples = expected_sources_for_date(date(2026, 5, 11))  # Monday
    assert ("finnhub", "earnings", "us") in triples
    assert ("dart", "disclosure", "kr") in triples
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_expected_sources_drops_finnhub_on_us_weekend() -> None:
    triples = expected_sources_for_date(date(2026, 5, 9))  # Saturday
    assert ("finnhub", "earnings", "us") not in triples
    # ForexFactory still publishes weekend data (rarely), so we still expect it.
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_expected_sources_drops_dart_on_kr_weekend() -> None:
    triples = expected_sources_for_date(date(2026, 5, 10))  # Sunday in KST
    assert ("dart", "disclosure", "kr") not in triples


@pytest.mark.unit
def test_expected_sources_constant_matches_per_day_union() -> None:
    # Every triple yielded by expected_sources_for_date must be a member of EXPECTED_SOURCES.
    for d in (date(2026, 5, 11), date(2026, 5, 9), date(2026, 5, 10)):
        for triple in expected_sources_for_date(d):
            assert triple in EXPECTED_SOURCES
```

- [ ] **Step 2: Run the test (must fail because the module doesn't exist)**

```bash
uv run pytest tests/services/test_market_events_expected_sources.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.market_events.expected_sources'`.

- [ ] **Step 3: Implement `expected_sources.py`**

```python
# app/services/market_events/expected_sources.py
"""Enumerates `(source, category, market)` triples expected to have data on a given date.

Pure function — no DB, no I/O. Used by the freshness service to distinguish
"never ingested" from "ingested but legitimately empty."

Weekend handling:
* US markets are closed Saturday + Sunday (UTC weekday 5 / 6); finnhub
  earningsCalendar still returns rows on weekends in rare cases (e.g. Berkshire
  weekend release) but for "expected" purposes we treat US weekends as not
  expected.
* KR markets follow the same Sat/Sun rule. We do not yet model KRX/NYSE
  observed holidays — the freshness signal for those days will simply show
  "no expected partition" rather than "missing." That's fine for the diagnostic
  surface; the dedicated KR-holidays source is a follow-up tracked in
  `docs/runbooks/calendar-source-coverage.md`.
* ForexFactory publishes a "this week" XML that always contains the upcoming
  five business days; we treat it as expected every day.
"""

from __future__ import annotations

from datetime import date

# All triples we currently know how to ingest (mirrors `SUPPORTED` in
# `scripts/ingest_market_events.py`).
EXPECTED_SOURCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
    }
)


def expected_sources_for_date(target_date: date) -> frozenset[tuple[str, str, str]]:
    """Return the subset of EXPECTED_SOURCES expected to have non-empty data on `target_date`.

    Saturday = 5, Sunday = 6 in `date.weekday()`.
    """
    weekday = target_date.weekday()
    is_weekend = weekday >= 5

    triples: set[tuple[str, str, str]] = {("forexfactory", "economic", "global")}
    if not is_weekend:
        triples.add(("finnhub", "earnings", "us"))
        triples.add(("dart", "disclosure", "kr"))
    return frozenset(triples)
```

- [ ] **Step 4: Run the test (must pass)**

```bash
uv run pytest tests/services/test_market_events_expected_sources.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/expected_sources.py tests/services/test_market_events_expected_sources.py
git commit -m "feat(rob-167): expected_sources pure module for calendar freshness baseline

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: `MarketEventsFreshnessService` + tests

**Files:**
- Create: `app/services/market_events/freshness_service.py`
- Create: `app/schemas/calendar_freshness.py`
- Create: `tests/services/test_market_events_freshness_service.py`

- [ ] **Step 1: Write the schema (no behavior, just types)**

```python
# app/schemas/calendar_freshness.py
"""Read-only freshness/coverage schemas for /invest/calendar diagnostics (ROB-167)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Per-day data state, derived from the expected partitions for that date:
#  - "loaded"       : every expected partition succeeded with event_count >= 0
#  - "empty"        : every expected partition succeeded but event_count == 0
#  - "partial"      : some expected partitions succeeded, some missing/failed
#  - "missing"      : zero partitions exist for this date (never ingested)
#  - "error"        : at least one expected partition is in failed state
#  - "stale"        : every expected partition succeeded but the most recent
#                     finished_at is older than the configured staleness window
CalendarDayState = Literal[
    "loaded", "empty", "partial", "missing", "error", "stale"
]

# Per-source aggregate freshness state across the requested range.
CalendarSourceState = Literal["fresh", "stale", "failed", "missing"]


class CalendarSourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    category: str
    market: str
    state: CalendarSourceState
    lastSuccessAt: datetime | None = None
    lastFailureAt: datetime | None = None
    lastError: str | None = None
    succeededPartitions: int = 0
    failedPartitions: int = 0
    missingPartitions: int = 0
    eventCount: int = 0


class CalendarCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromDate: date
    toDate: date
    expectedPartitions: int
    succeededPartitions: int
    failedPartitions: int
    missingPartitions: int
    totalEvents: int


class CoveragePartitionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    category: str
    market: str
    partitionDate: date
    status: Literal[
        "expected_missing", "pending", "running", "succeeded", "failed", "partial"
    ]
    eventCount: int = 0
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    lastError: str | None = None
    retryCount: int = 0


class CoverageMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromDate: date
    toDate: date
    asOf: datetime
    sources: list[CalendarSourceStatus] = Field(default_factory=list)
    partitions: list[CoveragePartitionRow] = Field(default_factory=list)
    coverage: CalendarCoverage
```

- [ ] **Step 2: Write the failing service test (DB-backed using async session fixture)**

> The test uses `db_session` from `tests/conftest.py`. If the async fixture is named differently, adjust to the project convention — search with `Grep("async def db_session", path="tests")`. Existing tests for the repo use `db_session` (see `tests/services/test_market_events_repository.py`).

```python
# tests/services/test_market_events_freshness_service.py
"""Unit tests for MarketEventsFreshnessService (ROB-167)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.freshness_service import (
    MarketEventsFreshnessService,
    STALE_AFTER_HOURS,
)


def _add_partition(
    db: AsyncSession,
    *,
    source: str,
    category: str,
    market: str,
    partition_date: date,
    status: str,
    event_count: int = 0,
    finished_at: datetime | None = None,
    last_error: str | None = None,
) -> MarketEventIngestionPartition:
    row = MarketEventIngestionPartition(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
        status=status,
        event_count=event_count,
        finished_at=finished_at,
        last_error=last_error,
        retry_count=0,
    )
    db.add(row)
    return row


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_partition_marks_day_missing(db_session: AsyncSession) -> None:
    monday = date(2026, 5, 11)
    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "missing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_succeeded_marks_day_loaded(db_session: AsyncSession) -> None:
    monday = date(2026, 5, 11)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    _add_partition(db_session, source="finnhub", category="earnings", market="us",
                   partition_date=monday, status="succeeded", event_count=12, finished_at=fresh)
    _add_partition(db_session, source="dart", category="disclosure", market="kr",
                   partition_date=monday, status="succeeded", event_count=4, finished_at=fresh)
    _add_partition(db_session, source="forexfactory", category="economic", market="global",
                   partition_date=monday, status="succeeded", event_count=3, finished_at=fresh)
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "loaded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_zero_event_count_marks_day_empty(db_session: AsyncSession) -> None:
    monday = date(2026, 5, 11)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    for src, cat, mkt in (
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
    ):
        _add_partition(db_session, source=src, category=cat, market=mkt,
                       partition_date=monday, status="succeeded", event_count=0,
                       finished_at=fresh)
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "empty"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_failed_marks_day_error(db_session: AsyncSession) -> None:
    monday = date(2026, 5, 11)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    _add_partition(db_session, source="finnhub", category="earnings", market="us",
                   partition_date=monday, status="succeeded", event_count=2, finished_at=fresh)
    _add_partition(db_session, source="dart", category="disclosure", market="kr",
                   partition_date=monday, status="failed", finished_at=fresh,
                   last_error="connection refused")
    _add_partition(db_session, source="forexfactory", category="economic", market="global",
                   partition_date=monday, status="succeeded", event_count=1, finished_at=fresh)
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_when_finished_at_older_than_window(db_session: AsyncSession) -> None:
    monday = date(2026, 5, 11)
    stale = datetime.now(UTC) - timedelta(hours=STALE_AFTER_HOURS + 2)
    for src, cat, mkt in (
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
    ):
        _add_partition(db_session, source=src, category=cat, market=mkt,
                       partition_date=monday, status="succeeded", event_count=1,
                       finished_at=stale)
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coverage_matrix_aggregates_by_source(db_session: AsyncSession) -> None:
    fresh = datetime.now(UTC) - timedelta(hours=1)
    monday = date(2026, 5, 11)
    tuesday = date(2026, 5, 12)
    _add_partition(db_session, source="finnhub", category="earnings", market="us",
                   partition_date=monday, status="succeeded", event_count=10, finished_at=fresh)
    _add_partition(db_session, source="finnhub", category="earnings", market="us",
                   partition_date=tuesday, status="failed", last_error="429",
                   finished_at=fresh)
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    matrix = await svc.get_coverage_matrix(monday, tuesday)

    finnhub_status = next(s for s in matrix.sources
                          if s.source == "finnhub" and s.market == "us")
    assert finnhub_status.succeededPartitions == 1
    assert finnhub_status.failedPartitions == 1
    assert finnhub_status.missingPartitions == 0  # both days have rows
    assert finnhub_status.eventCount == 10
    assert finnhub_status.state == "failed"
    assert finnhub_status.lastError == "429"
```

- [ ] **Step 3: Run tests (must fail because the service doesn't exist)**

```bash
uv run pytest tests/services/test_market_events_freshness_service.py -v
```

Expected: `ImportError: cannot import name 'MarketEventsFreshnessService'`.

- [ ] **Step 4: Implement the service**

```python
# app/services/market_events/freshness_service.py
"""Read-only freshness + coverage queries against market_event_ingestion_partitions (ROB-167).

This service NEVER writes. It is the canonical source of truth for the
diagnostic surfaces in `/invest/api/calendar` (CalendarMeta) and
`GET /trading/api/market-events/coverage`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventIngestionPartition
from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarDayState,
    CalendarSourceState,
    CalendarSourceStatus,
    CoverageMatrixResponse,
    CoveragePartitionRow,
)
from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)

# Partition is considered stale if its `finished_at` is older than this window.
# Sized for the recommended Prefect rolling window (today-7 .. today+60) being
# refreshed at least every 24 h.
STALE_AFTER_HOURS = 36


def _is_stale(finished_at: datetime | None, *, now: datetime) -> bool:
    if finished_at is None:
        return False
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return finished_at < (now - timedelta(hours=STALE_AFTER_HOURS))


def _date_iter(from_date: date, to_date: date):
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


class MarketEventsFreshnessService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_partitions(
        self, from_date: date, to_date: date
    ) -> list[MarketEventIngestionPartition]:
        stmt = (
            select(MarketEventIngestionPartition)
            .where(
                MarketEventIngestionPartition.partition_date >= from_date,
                MarketEventIngestionPartition.partition_date <= to_date,
            )
            .order_by(
                MarketEventIngestionPartition.partition_date.asc(),
                MarketEventIngestionPartition.source.asc(),
            )
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_per_day_states(
        self, from_date: date, to_date: date, *, now: datetime | None = None
    ) -> dict[date, CalendarDayState]:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        now = now or datetime.now(UTC)
        rows = await self._load_partitions(from_date, to_date)

        # rows grouped by (date) -> list[MarketEventIngestionPartition]
        by_date: dict[date, list[MarketEventIngestionPartition]] = defaultdict(list)
        for r in rows:
            by_date[r.partition_date].append(r)

        out: dict[date, CalendarDayState] = {}
        for d in _date_iter(from_date, to_date):
            expected = expected_sources_for_date(d)
            present = by_date.get(d, [])
            present_keys = {(p.source, p.category, p.market) for p in present}

            if not present:
                out[d] = "missing"
                continue

            missing = expected - present_keys
            failed = [p for p in present if p.status == "failed"]
            running_or_pending = [p for p in present if p.status in ("running", "pending")]
            succeeded = [p for p in present if p.status == "succeeded"]

            if failed:
                out[d] = "error"
                continue
            if missing or running_or_pending:
                out[d] = "partial"
                continue
            # All present and all succeeded
            if all(_is_stale(p.finished_at, now=now) for p in succeeded):
                out[d] = "stale"
                continue
            if all(p.event_count == 0 for p in succeeded):
                out[d] = "empty"
                continue
            out[d] = "loaded"

        return out

    async def get_coverage_matrix(
        self, from_date: date, to_date: date, *, now: datetime | None = None
    ) -> CoverageMatrixResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        now = now or datetime.now(UTC)
        rows = await self._load_partitions(from_date, to_date)

        # Per-source aggregation across the entire range.
        by_source: dict[tuple[str, str, str], dict[str, object]] = {}

        # Seed with every triple we know about so "never ingested" surfaces.
        for triple in EXPECTED_SOURCES:
            by_source[triple] = {
                "succeeded": 0,
                "failed": 0,
                "event_count": 0,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error": None,
            }
        for r in rows:
            triple = (r.source, r.category, r.market)
            agg = by_source.setdefault(
                triple,
                {
                    "succeeded": 0,
                    "failed": 0,
                    "event_count": 0,
                    "last_success_at": None,
                    "last_failure_at": None,
                    "last_error": None,
                },
            )
            if r.status == "succeeded":
                agg["succeeded"] = int(agg["succeeded"]) + 1
                agg["event_count"] = int(agg["event_count"]) + (r.event_count or 0)
                cur = agg["last_success_at"]
                if r.finished_at is not None and (cur is None or r.finished_at > cur):
                    agg["last_success_at"] = r.finished_at
            elif r.status == "failed":
                agg["failed"] = int(agg["failed"]) + 1
                cur = agg["last_failure_at"]
                if r.finished_at is not None and (cur is None or r.finished_at > cur):
                    agg["last_failure_at"] = r.finished_at
                    agg["last_error"] = r.last_error

        # Compute expected_partitions per triple = number of dates in range
        # where the triple is in expected_sources_for_date(d).
        expected_per_triple: dict[tuple[str, str, str], int] = defaultdict(int)
        for d in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(d):
                expected_per_triple[triple] += 1

        sources: list[CalendarSourceStatus] = []
        total_expected = 0
        total_succeeded = 0
        total_failed = 0
        total_missing = 0
        total_events = 0

        for triple in sorted(by_source.keys()):
            src, cat, mkt = triple
            agg = by_source[triple]
            expected_count = expected_per_triple.get(triple, 0)
            succeeded_count = int(agg["succeeded"])
            failed_count = int(agg["failed"])
            missing_count = max(0, expected_count - succeeded_count - failed_count)
            event_count = int(agg["event_count"])
            last_success_at: datetime | None = agg["last_success_at"]  # type: ignore[assignment]
            last_failure_at: datetime | None = agg["last_failure_at"]  # type: ignore[assignment]

            state: CalendarSourceState
            if failed_count > 0:
                state = "failed"
            elif succeeded_count == 0:
                state = "missing"
            elif last_success_at is not None and _is_stale(last_success_at, now=now):
                state = "stale"
            else:
                state = "fresh"

            sources.append(
                CalendarSourceStatus(
                    source=src,
                    category=cat,
                    market=mkt,
                    state=state,
                    lastSuccessAt=last_success_at,
                    lastFailureAt=last_failure_at,
                    lastError=str(agg["last_error"]) if agg["last_error"] is not None else None,
                    succeededPartitions=succeeded_count,
                    failedPartitions=failed_count,
                    missingPartitions=missing_count,
                    eventCount=event_count,
                )
            )
            total_expected += expected_count
            total_succeeded += succeeded_count
            total_failed += failed_count
            total_missing += missing_count
            total_events += event_count

        # Per-day partition rows for the matrix view.
        partitions: list[CoveragePartitionRow] = []
        present_keys: set[tuple[date, str, str, str]] = set()
        for r in rows:
            present_keys.add((r.partition_date, r.source, r.category, r.market))
            partitions.append(
                CoveragePartitionRow(
                    source=r.source,
                    category=r.category,
                    market=r.market,
                    partitionDate=r.partition_date,
                    status=r.status,  # type: ignore[arg-type]
                    eventCount=r.event_count,
                    startedAt=r.started_at,
                    finishedAt=r.finished_at,
                    lastError=r.last_error,
                    retryCount=r.retry_count,
                )
            )
        # Surface "expected but missing" partition slots so the UI can show them.
        for d in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(d):
                key = (d, triple[0], triple[1], triple[2])
                if key in present_keys:
                    continue
                partitions.append(
                    CoveragePartitionRow(
                        source=triple[0],
                        category=triple[1],
                        market=triple[2],
                        partitionDate=d,
                        status="expected_missing",
                    )
                )
        partitions.sort(
            key=lambda p: (p.partitionDate, p.source, p.category, p.market)
        )

        # Cross-check totalEvents against market_events for the range, useful
        # for spotting cases where partitions report events but rows are gone
        # (or vice-versa).
        actual_event_count = (
            await self.db.execute(
                select(func.count())
                .select_from(MarketEvent)
                .where(
                    MarketEvent.event_date >= from_date,
                    MarketEvent.event_date <= to_date,
                )
            )
        ).scalar_one()

        coverage = CalendarCoverage(
            fromDate=from_date,
            toDate=to_date,
            expectedPartitions=total_expected,
            succeededPartitions=total_succeeded,
            failedPartitions=total_failed,
            missingPartitions=total_missing,
            totalEvents=int(actual_event_count),
        )

        return CoverageMatrixResponse(
            fromDate=from_date,
            toDate=to_date,
            asOf=now,
            sources=sources,
            partitions=partitions,
            coverage=coverage,
        )
```

- [ ] **Step 5: Run tests (must pass)**

```bash
uv run pytest tests/services/test_market_events_freshness_service.py -v
```

Expected: 6 PASS. If any test fails because `db_session` is named differently, either rename the fixture in the test or rename and re-run. Do not silently swallow `ValueError` — surface it.

- [ ] **Step 6: Lint + format**

```bash
uv run ruff check app/services/market_events/freshness_service.py app/schemas/calendar_freshness.py tests/services/test_market_events_freshness_service.py
uv run ruff format app/services/market_events/freshness_service.py app/schemas/calendar_freshness.py tests/services/test_market_events_freshness_service.py
```

- [ ] **Step 7: Commit**

```bash
git add app/services/market_events/freshness_service.py app/schemas/calendar_freshness.py tests/services/test_market_events_freshness_service.py
git commit -m "feat(rob-167): MarketEventsFreshnessService + calendar freshness schemas

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Add `GET /trading/api/market-events/coverage` endpoint

**Files:**
- Modify: `app/routers/market_events.py`
- Create: `tests/test_market_events_coverage_router.py`

- [ ] **Step 1: Write the failing endpoint test**

```python
# tests/test_market_events_coverage_router.py
"""Endpoint test for GET /trading/api/market-events/coverage (ROB-167)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.models.market_events import MarketEventIngestionPartition


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coverage_endpoint_reports_succeeded_and_missing(
    authenticated_client: AsyncClient, db_session
) -> None:
    monday = date(2026, 5, 11)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    db_session.add(
        MarketEventIngestionPartition(
            source="finnhub",
            category="earnings",
            market="us",
            partition_date=monday,
            status="succeeded",
            event_count=7,
            finished_at=fresh,
            retry_count=0,
        )
    )
    await db_session.flush()
    await db_session.commit()

    res = await authenticated_client.get(
        "/trading/api/market-events/coverage",
        params={"from_date": monday.isoformat(), "to_date": monday.isoformat()},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["fromDate"] == monday.isoformat()
    assert data["toDate"] == monday.isoformat()
    sources = {(s["source"], s["category"], s["market"]): s for s in data["sources"]}
    assert sources[("finnhub", "earnings", "us")]["state"] == "fresh"
    assert sources[("finnhub", "earnings", "us")]["eventCount"] == 7
    assert sources[("dart", "disclosure", "kr")]["state"] == "missing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coverage_endpoint_rejects_inverted_range(
    authenticated_client: AsyncClient,
) -> None:
    res = await authenticated_client.get(
        "/trading/api/market-events/coverage",
        params={"from_date": "2026-05-12", "to_date": "2026-05-11"},
    )
    assert res.status_code == 400
```

> Use whatever fixture name the project already has for an authenticated httpx AsyncClient. `Grep("authenticated_client", path="tests/conftest.py")` to confirm; existing market-events router tests already use it (see `tests/test_market_events_router.py`).

- [ ] **Step 2: Run test (must fail)**

```bash
uv run pytest tests/test_market_events_coverage_router.py -v
```

Expected: 404 from missing route or 422 from missing schema.

- [ ] **Step 3: Add the endpoint**

Open `app/routers/market_events.py`. Add the import:

```python
from app.schemas.calendar_freshness import CoverageMatrixResponse
from app.services.market_events.freshness_service import MarketEventsFreshnessService
```

Add the route below the existing `get_discover_calendar`:

```python
@router.get(
    "/api/market-events/coverage",
    response_model=CoverageMatrixResponse,
)
async def get_market_events_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[date, Query(description="ISO end date, inclusive")],
) -> CoverageMatrixResponse:
    svc = MarketEventsFreshnessService(db)
    try:
        return await svc.get_coverage_matrix(from_date, to_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
```

- [ ] **Step 4: Run test (must pass)**

```bash
uv run pytest tests/test_market_events_coverage_router.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Lint + format**

```bash
uv run ruff check app/routers/market_events.py tests/test_market_events_coverage_router.py
uv run ruff format app/routers/market_events.py tests/test_market_events_coverage_router.py
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/market_events.py tests/test_market_events_coverage_router.py
git commit -m "feat(rob-167): GET /trading/api/market-events/coverage read-only endpoint

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 4: Extend `CalendarMeta` + `CalendarDay` schemas

**Files:**
- Modify: `app/schemas/invest_calendar.py`

- [ ] **Step 1: Update `app/schemas/invest_calendar.py`**

Add the import block at the top, just below the existing imports:

```python
from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarDayState,
    CalendarSourceStatus,
)
```

Replace the `CalendarMeta` class with:

```python
class CalendarMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[str] = Field(default_factory=list)
    sourceFreshness: list[CalendarSourceStatus] = Field(default_factory=list)
    coverage: CalendarCoverage | None = None
```

Replace the `CalendarDay` class with:

```python
class CalendarDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    events: list[CalendarEvent] = Field(default_factory=list)
    clusters: list[CalendarCluster] = Field(default_factory=list)
    dataState: CalendarDayState = "loaded"
```

> Note the default of `"loaded"` is intentional for backward compatibility — existing tests that don't construct `CalendarDay` with `dataState` keep working. `build_calendar` in Task 5 always sets it explicitly.

- [ ] **Step 2: Quick sanity test that pydantic still parses cleanly**

```bash
uv run python -c "from app.schemas.invest_calendar import CalendarResponse, CalendarDay, CalendarMeta; print(CalendarMeta().model_dump())"
```

Expected: `{'warnings': [], 'sourceFreshness': [], 'coverage': None}`.

- [ ] **Step 3: Commit**

```bash
git add app/schemas/invest_calendar.py
git commit -m "feat(rob-167): extend CalendarMeta + CalendarDay with freshness fields

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 5: Wire freshness into `build_calendar` + tests

**Files:**
- Modify: `app/services/invest_view_model/calendar_service.py`
- Modify: `tests/test_invest_calendar_router.py`

- [ ] **Step 1: Add the failing test cases** (append to `tests/test_invest_calendar_router.py`)

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_meta_includes_source_freshness(monkeypatch) -> None:
    from datetime import UTC, date, datetime
    from app.schemas.calendar_freshness import (
        CalendarCoverage,
        CalendarSourceStatus,
        CoverageMatrixResponse,
    )
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = []
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(
        return_value={date(2026, 5, 11): "missing"}
    )
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=CoverageMatrixResponse(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            asOf=datetime.now(UTC),
            sources=[
                CalendarSourceStatus(
                    source="finnhub", category="earnings", market="us",
                    state="missing",
                )
            ],
            partitions=[],
            coverage=CalendarCoverage(
                fromDate=date(2026, 5, 11),
                toDate=date(2026, 5, 11),
                expectedPartitions=3,
                succeededPartitions=0,
                failedPartitions=0,
                missingPartitions=3,
                totalEvents=0,
            ),
        )
    )
    monkeypatch.setattr(
        svc, "MarketEventsFreshnessService", lambda db: fake_freshness
    )

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 11),
        tab="all",
    )
    assert resp.days[0].dataState == "missing"
    assert len(resp.meta.sourceFreshness) == 1
    assert resp.meta.sourceFreshness[0].state == "missing"
    assert resp.meta.coverage is not None
    assert resp.meta.coverage.missingPartitions == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_marks_loaded_when_events_present(monkeypatch) -> None:
    from datetime import UTC, date, datetime
    from app.schemas.calendar_freshness import (
        CalendarCoverage,
        CoverageMatrixResponse,
    )
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [_fake_event(event_id="e1", ev_date=date(2026, 5, 11))]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(
        return_value={date(2026, 5, 11): "loaded"}
    )
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=CoverageMatrixResponse(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            asOf=datetime.now(UTC),
            sources=[],
            partitions=[],
            coverage=CalendarCoverage(
                fromDate=date(2026, 5, 11),
                toDate=date(2026, 5, 11),
                expectedPartitions=3,
                succeededPartitions=3,
                failedPartitions=0,
                missingPartitions=0,
                totalEvents=1,
            ),
        )
    )
    monkeypatch.setattr(
        svc, "MarketEventsFreshnessService", lambda db: fake_freshness
    )

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 11),
        tab="all",
    )
    assert resp.days[0].dataState == "loaded"
```

- [ ] **Step 2: Run test (must fail)**

```bash
uv run pytest tests/test_invest_calendar_router.py -v -k "freshness or marks_loaded"
```

Expected: AttributeError (no `MarketEventsFreshnessService` import in `calendar_service`) or assertion error on missing `dataState`.

- [ ] **Step 3: Modify `app/services/invest_view_model/calendar_service.py`**

Replace the import block (top of file, after `from sqlalchemy.ext.asyncio import AsyncSession`) with:

```python
from app.schemas.calendar_freshness import CalendarCoverage, CalendarSourceStatus
from app.schemas.invest_calendar import (
    Badge,
    CalendarCluster,
    CalendarDay,
    CalendarEvent,
    CalendarMarket,
    CalendarMeta,
    CalendarRelatedSymbol,
    CalendarResponse,
    CalendarTab,
    EventType,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_events.freshness_service import MarketEventsFreshnessService
from app.services.market_events.query_service import MarketEventsQueryService
```

Replace the body of `build_calendar` so it queries freshness alongside events. The existing per-day loop replaces its `CalendarDay(date=d, events=events, clusters=clusters)` call with the freshness-aware version below, and the `return CalendarResponse(...)` at the bottom is updated.

```python
async def build_calendar(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    from_date: date,
    to_date: date,
    tab: CalendarTab,
) -> CalendarResponse:
    svc = MarketEventsQueryService(db)
    freshness_svc = MarketEventsFreshnessService(db)

    range_resp = await svc.list_for_range(from_date, to_date)
    per_day_states = await freshness_svc.get_per_day_states(from_date, to_date)
    coverage_matrix = await freshness_svc.get_coverage_matrix(from_date, to_date)

    by_day: dict[date, list[CalendarEvent]] = {}
    for raw in getattr(range_resp, "events", []):
        # ... unchanged event-mapping body from the existing implementation ...
        # KEEP the existing logic verbatim — only the bottom of the function changes.
        market = _normalize_market(getattr(raw, "market", None))
        etype = _normalize_event_type(getattr(raw, "category", None))
        if tab != "all" and etype != tab:
            continue
        symbol = getattr(raw, "symbol", None)
        related: list[CalendarRelatedSymbol] = []
        relation = "none"
        if symbol and market in ("kr", "us", "crypto"):
            display_name = str(getattr(raw, "company_name", None) or symbol)
            related.append(
                CalendarRelatedSymbol(
                    symbol=str(symbol),
                    market=market,  # type: ignore[arg-type]
                    displayName=display_name,
                )
            )
            relation = resolver.relation(market, symbol)

        badges: list[Badge] = []
        if relation in ("held", "both"):
            badges.append("holdings")
        if relation in ("watchlist", "both"):
            badges.append("watchlist")

        event_id = str(
            getattr(raw, "source_event_id", None)
            or getattr(raw, "id", None)
            or ""
        )

        raw_values = getattr(raw, "values", None) or []
        first_val = raw_values[0] if raw_values else None
        actual = (
            str(getattr(first_val, "actual", None))
            if first_val and getattr(first_val, "actual", None) is not None
            else None
        )
        forecast = (
            str(getattr(first_val, "forecast", None))
            if first_val and getattr(first_val, "forecast", None) is not None
            else None
        )
        previous = (
            str(getattr(first_val, "previous", None))
            if first_val and getattr(first_val, "previous", None) is not None
            else None
        )

        event_time = getattr(raw, "release_time_utc", None)

        ev = CalendarEvent(
            eventId=event_id,
            title=str(getattr(raw, "title", "") or ""),
            market=market,
            eventType=etype,
            eventTimeLocal=event_time,
            source=str(getattr(raw, "source", "") or ""),
            actual=actual,
            forecast=forecast,
            previous=previous,
            relatedSymbols=related,
            relation=relation,  # type: ignore[arg-type]
            badges=badges,
        )
        ev_date = getattr(raw, "event_date", None) or (
            ev.eventTimeLocal.date() if ev.eventTimeLocal else from_date
        )
        by_day.setdefault(ev_date, []).append(ev)

    days: list[CalendarDay] = []
    for d in _date_range(from_date, to_date):
        events = by_day.get(d, [])
        clusters: list[CalendarCluster] = []
        if len(events) > CLUSTER_THRESHOLD:
            grouped: dict[tuple[EventType, CalendarMarket], list[CalendarEvent]] = {}
            for ev in events:
                grouped.setdefault((ev.eventType, ev.market), []).append(ev)
            kept: list[CalendarEvent] = []
            for (etype, market), group in grouped.items():
                if len(group) > 5:
                    clusters.append(
                        CalendarCluster(
                            clusterId=f"{d.isoformat()}:{etype}:{market}",
                            label=f"{etype} {market}".strip(),
                            eventType=etype,
                            market=market,
                            eventCount=len(group),
                            topEvents=group[:5],
                        )
                    )
                else:
                    kept.extend(group)
            events = kept
        day_state = per_day_states.get(d, "missing")
        days.append(
            CalendarDay(
                date=d,
                events=events,
                clusters=clusters,
                dataState=day_state,
            )
        )

    meta = CalendarMeta(
        sourceFreshness=list(coverage_matrix.sources),
        coverage=coverage_matrix.coverage,
    )

    return CalendarResponse(
        tab=tab,
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(UTC),
        days=days,
        meta=meta,
    )
```

> When making this change, replace the existing `_query_service` / `return CalendarResponse(...)` body wholesale with the version above; do not leave dead branches.

- [ ] **Step 4: Run the new tests (must pass)**

```bash
uv run pytest tests/test_invest_calendar_router.py -v
```

Expected: existing 2 tests still PASS, new 2 tests PASS (4 PASS total). The first two existing tests don't monkeypatch the freshness service — they will hit the real DB-less `MarketEventsFreshnessService` and crash. Fix this by either:

(a) updating the existing tests to also monkeypatch `MarketEventsFreshnessService` with a `_fake_freshness` returning empty results, **OR**
(b) refactoring the existing tests to use the new `monkeypatch` pattern.

Choose (a). Add at the top of each existing test (`test_calendar_returns_per_day` and `test_calendar_clusters_when_over_threshold`):

```python
    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(return_value={})
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=__import__(
            "app.schemas.calendar_freshness", fromlist=["CoverageMatrixResponse", "CalendarCoverage"]
        ).CoverageMatrixResponse(
            fromDate=date(2026, 5, 4),
            toDate=date(2026, 5, 4),
            asOf=__import__("datetime").datetime.now(__import__("datetime").UTC),
            sources=[],
            partitions=[],
            coverage=__import__(
                "app.schemas.calendar_freshness", fromlist=["CalendarCoverage"]
            ).CalendarCoverage(
                fromDate=date(2026, 5, 4),
                toDate=date(2026, 5, 4),
                expectedPartitions=0,
                succeededPartitions=0,
                failedPartitions=0,
                missingPartitions=0,
                totalEvents=0,
            ),
        )
    )
    monkeypatch.setattr(svc, "MarketEventsFreshnessService", lambda db: fake_freshness)
```

(The dynamic import is ugly but avoids reordering imports in the existing test module — feel free to clean up to a top-of-file import if it doesn't break the other tests.)

Re-run `uv run pytest tests/test_invest_calendar_router.py -v` until all 4 PASS.

- [ ] **Step 5: Lint + format + typecheck**

```bash
uv run ruff check app/services/invest_view_model/calendar_service.py app/schemas/invest_calendar.py tests/test_invest_calendar_router.py
uv run ruff format app/services/invest_view_model/calendar_service.py app/schemas/invest_calendar.py tests/test_invest_calendar_router.py
uv run ty check app/services/invest_view_model/calendar_service.py app/schemas/invest_calendar.py
```

If `ty` reports type errors caused by `MarketEventResponse` attribute access via `getattr`, ignore — those predate this task.

- [ ] **Step 6: Commit**

```bash
git add app/services/invest_view_model/calendar_service.py app/schemas/invest_calendar.py tests/test_invest_calendar_router.py
git commit -m "feat(rob-167): wire freshness + per-day dataState into /invest/api/calendar

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 6: Frontend types + VM helpers

**Files:**
- Modify: `frontend/invest/src/types/calendar.ts`
- Modify: `frontend/invest/src/components/calendar/vm.ts`
- Create: `frontend/invest/src/__tests__/calendarFreshnessVm.test.ts`

- [ ] **Step 1: Extend `frontend/invest/src/types/calendar.ts`**

Add new types and extend the existing interfaces:

```ts
export type CalendarSourceState = "fresh" | "stale" | "failed" | "missing";
export type CalendarDayState =
  | "loaded"
  | "empty"
  | "partial"
  | "missing"
  | "error"
  | "stale";

export interface CalendarSourceStatus {
  source: string;
  category: string;
  market: string;
  state: CalendarSourceState;
  lastSuccessAt?: string | null;
  lastFailureAt?: string | null;
  lastError?: string | null;
  succeededPartitions: number;
  failedPartitions: number;
  missingPartitions: number;
  eventCount: number;
}

export interface CalendarCoverage {
  fromDate: string;
  toDate: string;
  expectedPartitions: number;
  succeededPartitions: number;
  failedPartitions: number;
  missingPartitions: number;
  totalEvents: number;
}
```

Update `CalendarDay`:

```ts
export interface CalendarDay {
  date: string;
  events: CalendarEvent[];
  clusters: CalendarCluster[];
  dataState: CalendarDayState;
}
```

Update `CalendarResponse.meta`:

```ts
export interface CalendarResponse {
  tab: CalendarTab;
  fromDate: string;
  toDate: string;
  asOf: string;
  days: CalendarDay[];
  meta: {
    warnings: string[];
    sourceFreshness: CalendarSourceStatus[];
    coverage: CalendarCoverage | null;
  };
}
```

- [ ] **Step 2: Add VM helpers to `frontend/invest/src/components/calendar/vm.ts`**

Append at the bottom:

```ts
import type { CalendarDayState, CalendarSourceStatus } from "../../types/calendar";

export function dataStateLabel(state: CalendarDayState): string {
  switch (state) {
    case "loaded":
      return "최신";
    case "empty":
      return "일정 없음";
    case "partial":
      return "일부 수집 중";
    case "missing":
      return "미수집";
    case "error":
      return "수집 실패";
    case "stale":
      return "오래된 데이터";
  }
}

export function freshnessBadgeLabel(status: CalendarSourceStatus): string {
  const sourceLabel: Record<string, string> = {
    finnhub: "Finnhub 실적",
    dart: "DART 공시",
    forexfactory: "ForexFactory 경제지표",
  };
  const label = sourceLabel[status.source] ?? status.source;
  switch (status.state) {
    case "fresh":
      return `${label} · 최신`;
    case "stale":
      return `${label} · 오래됨`;
    case "failed":
      return `${label} · 수집 실패`;
    case "missing":
      return `${label} · 미수집`;
  }
}
```

- [ ] **Step 3: Write the failing VM test**

```ts
// frontend/invest/src/__tests__/calendarFreshnessVm.test.ts
import { describe, expect, test } from "vitest";
import { dataStateLabel, freshnessBadgeLabel } from "../components/calendar/vm";

describe("calendar freshness VM helpers", () => {
  test("dataStateLabel covers every state", () => {
    expect(dataStateLabel("loaded")).toBe("최신");
    expect(dataStateLabel("empty")).toBe("일정 없음");
    expect(dataStateLabel("partial")).toBe("일부 수집 중");
    expect(dataStateLabel("missing")).toBe("미수집");
    expect(dataStateLabel("error")).toBe("수집 실패");
    expect(dataStateLabel("stale")).toBe("오래된 데이터");
  });

  test("freshnessBadgeLabel formats by source + state", () => {
    expect(
      freshnessBadgeLabel({
        source: "finnhub",
        category: "earnings",
        market: "us",
        state: "fresh",
        succeededPartitions: 5,
        failedPartitions: 0,
        missingPartitions: 0,
        eventCount: 23,
      }),
    ).toBe("Finnhub 실적 · 최신");

    expect(
      freshnessBadgeLabel({
        source: "dart",
        category: "disclosure",
        market: "kr",
        state: "failed",
        succeededPartitions: 1,
        failedPartitions: 2,
        missingPartitions: 0,
        eventCount: 0,
      }),
    ).toBe("DART 공시 · 수집 실패");
  });
});
```

- [ ] **Step 4: Run frontend tests**

```bash
cd frontend/invest && npm test -- --run calendarFreshnessVm
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-167-calendar-data-freshness
git add frontend/invest/src/types/calendar.ts frontend/invest/src/components/calendar/vm.ts frontend/invest/src/__tests__/calendarFreshnessVm.test.ts
git commit -m "feat(rob-167): frontend types + VM helpers for calendar freshness

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 7: Render freshness banner on Desktop + Mobile calendar

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx`
- Modify: `frontend/invest/src/pages/mobile/MobileCalendarPage.tsx`

A non-intrusive banner above the day list: only renders when at least one source is `failed`/`stale`/`missing`. Click target: none (display-only — read-only safety boundary).

- [ ] **Step 1: Add a small banner component inline in `DesktopCalendarPage.tsx`**

Just above the `Card` that renders `daySections`, insert:

```tsx
{calendar?.meta?.sourceFreshness && (
  <CalendarFreshnessBanner sources={calendar.meta.sourceFreshness} />
)}
```

At the bottom of the file (after `SegPill`), add:

```tsx
function CalendarFreshnessBanner({ sources }: { sources: CalendarSourceStatus[] }) {
  const stale = sources.filter((s) => s.state !== "fresh");
  if (stale.length === 0) return null;
  return (
    <div
      data-testid="calendar-freshness-banner"
      role="status"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        padding: "8px 12px",
        background: "var(--surface-2)",
        borderRadius: 10,
        fontSize: 12,
        color: "var(--fg-2)",
      }}
    >
      <span style={{ fontWeight: 700 }}>데이터 상태:</span>
      {stale.map((s) => (
        <span
          key={`${s.source}-${s.category}-${s.market}`}
          data-source={s.source}
          data-state={s.state}
          style={{
            padding: "2px 8px",
            borderRadius: 999,
            background: s.state === "failed" ? "var(--danger-soft)" : "var(--surface-3)",
            color: s.state === "failed" ? "var(--danger)" : "var(--fg-2)",
            fontWeight: 600,
          }}
        >
          {freshnessBadgeLabel(s)}
        </span>
      ))}
    </div>
  );
}
```

Add the matching import at the top:

```tsx
import { freshnessBadgeLabel } from "../../components/calendar/vm";
import type { CalendarSourceStatus } from "../../types/calendar";
```

> If your CSS variables don't include `--danger-soft` or `--surface-3`, fall back to inline hex values matching the existing palette (search `var(--surface)` in this file to copy the convention).

- [ ] **Step 2: Same banner in `MobileCalendarPage.tsx`**

Just above the `<div data-testid="day-events">` block, render:

```tsx
{calendar?.meta?.sourceFreshness && (
  <CalendarFreshnessBanner sources={calendar.meta.sourceFreshness} />
)}
```

Add the same `CalendarFreshnessBanner` function (or extract to `frontend/invest/src/components/calendar/CalendarFreshnessBanner.tsx` and import from both pages — preferred if you want zero duplication, otherwise inline both).

> Recommend extracting to `frontend/invest/src/components/calendar/CalendarFreshnessBanner.tsx` to avoid two copies. The component is tiny — keep one source of truth.

- [ ] **Step 3: Quick browser smoke** (per CLAUDE.md "UI changes — start the dev server")

```bash
cd frontend/invest && npm run dev
# In a second terminal, open http://localhost:5173/invest/calendar
# Verify the banner only renders when at least one source is non-fresh.
# Tear down dev server with Ctrl-C when done.
```

Document outcome in the PR (manual smoke is OK if dev server can't run in your environment — say so explicitly).

- [ ] **Step 4: Run existing frontend tests to confirm no regressions**

```bash
cd frontend/invest && npm test -- --run
```

Expected: all PASS. If `DesktopCalendarPage.test.tsx` snapshots change, regenerate them with `npm test -- --run -u` and inspect the diff to confirm only the banner area changed.

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-167-calendar-data-freshness
git add frontend/invest/src/pages/desktop/DesktopCalendarPage.tsx frontend/invest/src/pages/mobile/MobileCalendarPage.tsx
# (and the extracted CalendarFreshnessBanner.tsx if you went that route)
git add frontend/invest/src/components/calendar/CalendarFreshnessBanner.tsx 2>/dev/null || true
git commit -m "feat(rob-167): render calendar freshness banner on desktop + mobile

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 8: Read-only diagnostic CLI + smoke test

**Files:**
- Create: `scripts/diagnose_calendar_coverage.py`
- Create: `tests/test_diagnose_calendar_coverage_cli.py`

- [ ] **Step 1: Write the failing CLI test**

```python
# tests/test_diagnose_calendar_coverage_cli.py
"""Smoke test for the read-only diagnose_calendar_coverage CLI (ROB-167)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarSourceStatus,
    CoverageMatrixResponse,
)
from scripts import diagnose_calendar_coverage as cli


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cli_prints_json_summary(monkeypatch, capsys) -> None:
    fake_matrix = CoverageMatrixResponse(
        fromDate=date(2026, 5, 11),
        toDate=date(2026, 5, 11),
        asOf=datetime.now(UTC),
        sources=[
            CalendarSourceStatus(
                source="finnhub",
                category="earnings",
                market="us",
                state="fresh",
                lastSuccessAt=datetime.now(UTC) - timedelta(hours=1),
                succeededPartitions=1,
                failedPartitions=0,
                missingPartitions=0,
                eventCount=12,
            ),
        ],
        partitions=[],
        coverage=CalendarCoverage(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            expectedPartitions=3,
            succeededPartitions=1,
            failedPartitions=0,
            missingPartitions=2,
            totalEvents=12,
        ),
    )
    fake_svc = AsyncMock()
    fake_svc.get_coverage_matrix = AsyncMock(return_value=fake_matrix)
    monkeypatch.setattr(cli, "MarketEventsFreshnessService", lambda db: fake_svc)

    rc = await cli.run(from_date=date(2026, 5, 11), to_date=date(2026, 5, 11), as_json=True)

    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert rc == 0
    assert payload["coverage"]["expectedPartitions"] == 3
    assert payload["coverage"]["missingPartitions"] == 2
```

- [ ] **Step 2: Run the test (must fail)**

```bash
uv run pytest tests/test_diagnose_calendar_coverage_cli.py -v
```

Expected: `ModuleNotFoundError: No module named 'scripts.diagnose_calendar_coverage'`.

- [ ] **Step 3: Implement `scripts/diagnose_calendar_coverage.py`**

```python
#!/usr/bin/env python3
"""Read-only calendar coverage / freshness diagnostic CLI (ROB-167).

Prints a per-source freshness summary and per-day partition matrix for
[from_date, to_date]. NEVER writes to the database; safe to run against
production.

Examples:
    uv run python -m scripts.diagnose_calendar_coverage \
        --from-date 2026-05-11 --to-date 2026-05-17

    uv run python -m scripts.diagnose_calendar_coverage \
        --from-date 2026-05-11 --to-date 2026-05-17 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, date, datetime

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.market_events.freshness_service import (
    MarketEventsFreshnessService,
)

logger = logging.getLogger(__name__)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only calendar coverage diagnostic CLI (ROB-167)."
    )
    parser.add_argument("--from-date", required=True, type=_parse_iso, dest="from_date")
    parser.add_argument("--to-date", required=True, type=_parse_iso, dest="to_date")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%MZ")


def _print_human(matrix) -> None:
    print(f"\nCalendar coverage: {matrix.fromDate}..{matrix.toDate} "
          f"(asOf {_fmt_dt(matrix.asOf)})")
    print(f"  expected={matrix.coverage.expectedPartitions} "
          f"succeeded={matrix.coverage.succeededPartitions} "
          f"failed={matrix.coverage.failedPartitions} "
          f"missing={matrix.coverage.missingPartitions} "
          f"events={matrix.coverage.totalEvents}\n")
    print("Source freshness:")
    print(f"  {'source':<14} {'category':<11} {'mkt':<7} {'state':<8} "
          f"{'succ':>5} {'fail':>5} {'miss':>5} {'events':>7} last_success")
    for s in matrix.sources:
        print(
            f"  {s.source:<14} {s.category:<11} {s.market:<7} {s.state:<8} "
            f"{s.succeededPartitions:>5} {s.failedPartitions:>5} "
            f"{s.missingPartitions:>5} {s.eventCount:>7} "
            f"{_fmt_dt(s.lastSuccessAt)}"
        )
        if s.lastError:
            print(f"      lastError: {s.lastError}")
    print("\nPartitions:")
    print(f"  {'date':<11} {'source':<14} {'category':<11} {'mkt':<7} "
          f"{'status':<18} {'events':>6} finished_at")
    for p in matrix.partitions:
        print(
            f"  {p.partitionDate.isoformat():<11} {p.source:<14} "
            f"{p.category:<11} {p.market:<7} {p.status:<18} "
            f"{p.eventCount:>6} {_fmt_dt(p.finishedAt)}"
        )


async def run(*, from_date: date, to_date: date, as_json: bool) -> int:
    async with AsyncSessionLocal() as db:
        svc = MarketEventsFreshnessService(db)
        matrix = await svc.get_coverage_matrix(from_date, to_date)
    if as_json:
        print(matrix.model_dump_json())
    else:
        _print_human(matrix)
    return 0


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="diagnose-calendar-coverage")
    ns = parse_args(argv)
    try:
        return await run(
            from_date=ns.from_date, to_date=ns.to_date, as_json=ns.as_json
        )
    except Exception as exc:
        logger.exception("diagnose_calendar_coverage crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run the CLI test (must pass)**

```bash
uv run pytest tests/test_diagnose_calendar_coverage_cli.py -v
```

Expected: 1 PASS.

- [ ] **Step 5: Manual smoke (skip if no local DB)**

```bash
uv run python -m scripts.diagnose_calendar_coverage \
  --from-date 2026-05-11 --to-date 2026-05-17
```

Expected: prints the human-readable matrix. If your DB is empty, all sources read `state=missing` — correct behavior.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check scripts/diagnose_calendar_coverage.py tests/test_diagnose_calendar_coverage_cli.py
uv run ruff format scripts/diagnose_calendar_coverage.py tests/test_diagnose_calendar_coverage_cli.py
git add scripts/diagnose_calendar_coverage.py tests/test_diagnose_calendar_coverage_cli.py
git commit -m "feat(rob-167): scripts/diagnose_calendar_coverage read-only diagnostic CLI

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 9: Coverage gap matrix runbook

**Files:**
- Create: `docs/runbooks/calendar-source-coverage.md`
- Modify: `docs/runbooks/market-events-ingestion.md`

This is the deliverable that satisfies the spec's "if implementation is too risky, produce a concrete follow-up matrix" requirement. We did implement low-risk diagnostics, but the *coverage gaps* (KR holidays, dividends, IPOs, KR earnings schedules, crypto majors) remain follow-ups and need a concrete plan.

- [ ] **Step 1: Create `docs/runbooks/calendar-source-coverage.md`**

```markdown
# Calendar Source Coverage & Follow-ups (ROB-167)

> Read-only diagnostic surface lives in
> `app/services/market_events/freshness_service.py` and is exposed via
> `GET /trading/api/market-events/coverage` plus the `meta.sourceFreshness`
> block on `GET /invest/api/calendar`. CLI: `python -m scripts.diagnose_calendar_coverage`.

## What we ingest today

| Source | Category | Market | Ingest entry point | Notes |
| --- | --- | --- | --- | --- |
| Finnhub | earnings | us | `app/services/market_events/finnhub_helpers.py::fetch_earnings_calendar_finnhub` | Per-day partition. EPS / revenue / fiscal period. `time_hint` = bmo/amc/dmh. |
| DART | disclosure (and `earnings` when title matches) | kr | `app/services/market_events/dart_helpers.py::fetch_dart_filings_for_date` | Uses `OpenDartReader.list_date`. Symbol from `stock_code`. Title-classified into earnings vs. disclosure via `normalize_dart_disclosure_row`. |
| ForexFactory | economic | global | `app/services/market_events/forexfactory_helpers.py::fetch_forexfactory_events_for_date` | This-week + next-week XML. Times converted ET → UTC. |

`scripts/ingest_market_events.py::SUPPORTED` is the canonical list of supported triples.
`app/services/market_events/expected_sources.py::EXPECTED_SOURCES` mirrors it.

## Gaps the spec calls out

These categories are **not** ingested today. Each row describes the gap, the recommended source, license/access notes, and the follow-up safety plan.

### KR market holidays

| Field | Value |
| --- | --- |
| Why we need it | Calendar shouldn't render "수집 실패" on KRX-closed days; we want an explicit "휴장" badge. |
| Current behavior | `expected_sources_for_date` only drops Sat/Sun; KRX observed holidays appear as `partial`/`error`. |
| Recommended source | KRX official holiday calendar (`http://open.krx.co.kr/contents/MMC/STAT/holiday/MMCSTAT003.cmd`) — public, daily HTML/JSON. Backup: Korea Exchange XLS export from KOFIA. |
| License | Public (KRX 공시 정보 — open). |
| Follow-up Linear | TBD: "ROB-XXX: ingest KRX holiday calendar into market_events as `category=holiday, market=kr`" |
| Safety | Ingestion only. Per-day partition. No broker/order side effects. |

### Dividends / ex-dividend dates

| Field | Value |
| --- | --- |
| Why we need it | Major dividend dates drive watchlist alerts; absent today. |
| Recommended source | Finnhub `stock/dividend2` endpoint (already key-authorised) for US; KRX `stock/divDistConfReq` for KR. |
| License | Finnhub: paid tier we already use; KRX: public. |
| Follow-up Linear | "ROB-XXX: ingest US + KR dividend calendars". |
| Safety | Read-only; per-symbol fetch; should be batched, not fanned out per request. |

### KR earnings schedule (forward-looking)

| Field | Value |
| --- | --- |
| Why we need it | DART only records *released* earnings (잠정실적 등); we don't have a forward earnings calendar for KR companies. |
| Recommended source | NAVER Finance "실적 발표 일정" or `Investing.com` KR earnings calendar (scraping); paid: WiseFn. |
| License | NAVER: scraping caveats — keep low-frequency; Investing.com: ToS limits scraping. WiseFn: paid contract required. |
| Follow-up Linear | "ROB-XXX: research forward KR earnings schedule data source (license review + spike)". |
| Safety | If scraping route taken, must respect robots.txt + rate limits. Add to ingestion partition table for retry visibility. |

### IPO / public offering schedule

| Field | Value |
| --- | --- |
| Why we need it | "이번 주 IPO" surface for Discover. |
| Recommended source | KRX 공시 (already in DART under specific report types: 증권신고서(지분증권), 투자설명서); we can add a dedicated normalizer that keys off `report_nm` patterns. US: SEC EDGAR S-1 filings. |
| License | DART/SEC: public. |
| Follow-up Linear | "ROB-XXX: extend DART normalizer with `category=ipo` keyword set; add SEC EDGAR S-1 ingestor". |
| Safety | Pure normalizer addition — no new external API. Lowest-risk follow-up; could be rolled into a dedicated PR. |

### Crypto major events

| Field | Value |
| --- | --- |
| Why we need it | Taxonomy already supports `crypto_exchange_notice`, `crypto_protocol`, `tokenomics`, `regulatory`; no source connected. |
| Recommended source | Upbit `notices` API + Bithumb `notice` API for KR; CoinMarketCal API (partner license) for global tokenomics events. |
| License | Upbit/Bithumb: public RSS-style endpoints. CoinMarketCal: API key + ToS review needed. |
| Follow-up Linear | "ROB-XXX: ingest Upbit + Bithumb notices into market_events; spike CoinMarketCal license". |
| Safety | Crypto sources only — no broker mutation. Crypto trading already paper-only behind safety boundary. |

## Causes-of-empty-day taxonomy (used by freshness service)

| Day state | Trigger | UI label |
| --- | --- | --- |
| `loaded` | All expected partitions succeeded with at least one event row | "최신" |
| `empty` | All expected partitions succeeded with zero rows | "일정 없음" |
| `partial` | Some expected partitions succeeded, others missing or running | "일부 수집 중" |
| `missing` | Zero partitions exist for the date | "미수집" |
| `error` | At least one expected partition is in `failed` state | "수집 실패" |
| `stale` | All expected partitions succeeded but newest `finished_at` is older than `STALE_AFTER_HOURS` (36h) | "오래된 데이터" |

## Timezone notes

* `MarketEvent.event_date` is stored in source-native day:
  * Finnhub: UTC (Finnhub publishes ISO date)
  * DART: KST date (parsed from `rcept_dt`)
  * ForexFactory: ET date (we filter rows whose ET-day matches the requested date)
* `release_time_utc` is the UTC point-in-time when available.
* `/invest/api/calendar` queries by `event_date` directly — there is no extra TZ shift in `build_calendar`. If the UI shows an event "on the wrong day," the right place to look is the source-side ET → UTC conversion in `forexfactory_helpers._parse_one_xml`, not the calendar assembler.

## Display-hiding caveats

* Per-day cluster collapse threshold: `CLUSTER_THRESHOLD = 10` and per-(eventType, market) groups > 5 collapse into a `CalendarCluster.topEvents[:5]` (see `app/services/invest_view_model/calendar_service.py:25,134`).
* Mobile per-day visible limit: `PER_DAY_VISIBLE_LIMIT = 8` with surplus surfaced as `hidden_count` (see `app/services/market_events/discover_calendar.py:20`).

When investigating "why isn't event X visible on day Y": check `dataState` first, then check whether it ended up inside a cluster.

## Operating safely

* **Do not** enable a recurring ingestion schedule from this PR — that is gated by ROB-128 follow-ups.
* **Do not** call live source APIs from CI. The diagnostic CLI talks to the DB only.
* When running the diagnostic CLI against production, no DB writes occur; the SQL is `SELECT ... FROM market_event_ingestion_partitions` + `SELECT count(*) FROM market_events`.
```

- [ ] **Step 2: Append a pointer in `docs/runbooks/market-events-ingestion.md`**

Find the "Follow-ups (out of scope for this PR)" section and add at the bottom:

```markdown
6. **Calendar source coverage gaps** — see [`calendar-source-coverage.md`](./calendar-source-coverage.md) for the full gap matrix (KR holidays, dividends, IPO/subscription, forward KR earnings schedule, crypto majors) and the read-only freshness diagnostics (ROB-167).
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/calendar-source-coverage.md docs/runbooks/market-events-ingestion.md
git commit -m "docs(rob-167): calendar source-coverage gap matrix + freshness runbook

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 10: Final verification + push

- [ ] **Step 1: Run the full backend test slice**

```bash
uv run pytest tests/services/test_market_events_expected_sources.py \
              tests/services/test_market_events_freshness_service.py \
              tests/test_invest_calendar_router.py \
              tests/test_market_events_coverage_router.py \
              tests/test_diagnose_calendar_coverage_cli.py \
              -v
```

Expected: all PASS.

- [ ] **Step 2: Run the broader market-events suite to confirm no regressions**

```bash
uv run pytest tests/services/test_market_events_*.py tests/test_market_events_*.py -v
```

Expected: all PASS.

- [ ] **Step 3: Frontend tests + typecheck + build**

```bash
cd frontend/invest
npm test -- --run
npm run typecheck
npm run build
```

All must succeed. If snapshots changed in `DesktopCalendarPage.test.tsx`, regenerate (`npm test -- --run -u`) and confirm only the freshness banner area changed.

- [ ] **Step 4: Lint + format root project**

```bash
cd /Users/mgh3326/worktrees/auto_trader/ROB-167-calendar-data-freshness
uv run ruff check .
uv run ruff format --check .
uv run ty check app/services/market_events/freshness_service.py app/services/invest_view_model/calendar_service.py
```

- [ ] **Step 5: Push branch**

```bash
git push -u origin kanban/ROB-167-calendar-data-freshness
```

- [ ] **Step 6: Hand off to integrator with the following PR description body draft**

```text
## Summary
- Adds read-only freshness/coverage diagnostics to /invest/calendar so empty days can be classified (loaded / empty / partial / missing / error / stale).
- New endpoint: GET /trading/api/market-events/coverage (read-only).
- New CLI: scripts/diagnose_calendar_coverage.py (read-only).
- Documents source coverage gaps (KR holidays, dividends, IPO, forward KR earnings, crypto majors) with concrete follow-up source/license/safety notes in docs/runbooks/calendar-source-coverage.md.

## Safety
- No DB schema migrations.
- No broker / order / watch / scheduling / ingestion-enablement side effects.
- All new code paths are SELECT-only (ingestion partition + market_events count).

## Test plan
- [ ] uv run pytest tests/services/test_market_events_expected_sources.py tests/services/test_market_events_freshness_service.py tests/test_invest_calendar_router.py tests/test_market_events_coverage_router.py tests/test_diagnose_calendar_coverage_cli.py -v
- [ ] (cd frontend/invest && npm test -- --run && npm run typecheck && npm run build)
- [ ] uv run ruff check . && uv run ruff format --check .
- [ ] Manual smoke on /invest/calendar: confirm freshness banner only renders when at least one source is non-fresh.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## Acceptance checkpoints (mapped from the Linear spec)

| Spec acceptance criterion | How this plan satisfies it | Where to verify |
| --- | --- | --- |
| Clear coverage/freshness diagnosis for /invest/calendar monthly data | `MarketEventsFreshnessService.get_coverage_matrix` + per-day `dataState` | Task 2, Task 5 |
| UI/API distinguishes loading/error/empty/stale | `CalendarDayState` literal + frontend banner | Task 4, Task 7 |
| Calendar source gaps documented with concrete follow-ups | `docs/runbooks/calendar-source-coverage.md` | Task 9 |
| Implemented diagnostics are read-only and tested | `MarketEventsFreshnessService` only SELECTs; new tests cover it | Task 2, Task 3 |
| Frontend/backend tests pass | Tasks 1–8 each end with passing tests; Task 10 runs the full slice | Task 10 |
| PR is created from the dedicated git worktree branch | `kanban/ROB-167-calendar-data-freshness` only — Task 0 fast-forwards | Task 0, Task 10 |
| Read-only production deploy/smoke after merge | Diagnostic CLI is the smoke (no writes); GET /trading/api/market-events/coverage is read-only | Integrator (K4) responsibility |

## Risk notes

1. **`db_session` fixture name** — if the project's test fixture for an async session is named differently (`async_session`, `db`, etc.), the repository tests in Task 2 and Task 3 will need that name. The existing market-events tests already use this fixture; copy the pattern from `tests/services/test_market_events_repository.py`.
2. **`authenticated_client` fixture name** — same caveat for Task 3's endpoint test. Pattern is already used in `tests/test_market_events_router.py`.
3. **Test isolation against shared partitions** — Task 2 + Task 3 tests insert `MarketEventIngestionPartition` rows. The shared `db_session` fixture must roll back per test (already the case for ROB-128 tests). If you see cross-test pollution, scope rows with a unique date that no other test uses.
4. **Snapshot churn** — Task 7 may rebaseline `DesktopCalendarPage.test.tsx`. Inspect the snapshot diff carefully to ensure only the freshness banner area changed; do not blanket-update unrelated snapshots.
5. **`expected_sources` approximation** — KRX/NYSE observed holidays (Lunar New Year, Independence Day, etc.) are not modeled in Task 1. On those days the freshness service will report `partial` instead of "no expected partition." This is acceptable for V1 — the gap matrix doc explicitly tracks the KR holidays follow-up.
6. **`STALE_AFTER_HOURS = 36`** — chosen for the recommended Prefect rolling window (today-7 .. today+60) refreshed daily. If the actual cadence differs, adjust the constant in `freshness_service.py`. Avoid making it env-configurable in V1 — every config knob is a footgun.
7. **`MarketEventResponse.event_date` source-native day** — `build_calendar` already trusts `raw.event_date` (no TZ shift). If the calendar surfaces an event on the "wrong" day, the bug is in source-side normalization, not in this plan's scope. Document the symptom in the coverage runbook (already done in Task 9 → "Timezone notes").
8. **Backwards-compatibility of `CalendarMeta`** — adding fields with defaults is forwards-compatible for existing frontend consumers; `extra="forbid"` will reject unknown server-side keys, so the schema additions in Task 4 must land before the frontend types in Task 6 are merged. The single-PR plan keeps them in lockstep.

## Self-review

- **Spec coverage:** every Linear acceptance criterion maps to a task (see acceptance table above). Each spec-listed missing source category (KR holidays, dividends, IPO, KR earnings schedule, crypto majors) is in the gap matrix in Task 9.
- **Placeholders:** No "TBD" or "implement later" in implementation steps. Two follow-up Linear IDs in the runbook are intentionally open (`ROB-XXX`); they're not work this plan owns.
- **Type consistency:** `CalendarSourceStatus`, `CalendarCoverage`, `CoverageMatrixResponse`, `CoveragePartitionRow`, `CalendarDayState`, `CalendarSourceState` use identical field names backend ↔ frontend (camelCase via Pydantic field aliases? — Pydantic v2 with `model_config = ConfigDict(extra="forbid")` and explicit camelCase field names matches the existing convention in `app/schemas/invest_calendar.py`). Verified `dataState`, `sourceFreshness`, `coverage` are consistent.
- **DRY:** `expected_sources_for_date` is the single source of truth for "expected" coverage and is reused by the freshness service + CLI. `freshnessBadgeLabel` is the single source for source-state UI labels.
- **YAGNI:** No env-configurable staleness window; no historical trend chart; no per-symbol freshness; no auto-retry from the diagnostic CLI. All deferred to follow-ups.
- **TDD:** Every code task starts with a failing test.
- **Frequent commits:** 9 commits across the plan (one per task except Task 0).

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-10-rob-167-calendar-data-source-freshness.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — dispatch a fresh implementer per task, integrator review between tasks.
2. **Inline Execution** — execute tasks in this session using `superpowers:executing-plans` with a checkpoint after Task 5 (the schema/back-end pivot point).

**Which approach?**
