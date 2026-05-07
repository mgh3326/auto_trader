# ROB-132: Discover 오늘 이벤트 + ForexFactory Economic Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `/invest/app` Discover `TodayEventCard` to the existing `market_events` API, and add durable ForexFactory economic-calendar ingestion (`category=economic`) with forecast/previous/actual values.

**Architecture:**
1. Backend: Reuse the ROB-128 `MarketEventsRepository` / `ingest_*_for_date` shape. Add a new `forexfactory_helpers.py` (per-day XML fetch), a new `normalize_forexfactory_event_row` (pure normalizer), and a new `ingest_economic_events_for_date` orchestrator. Extend the CLI's `SUPPORTED` matrix and arg `choices`. Add a `currency` column via Alembic.
2. Frontend: Add `api/marketEvents.ts` + `hooks/useMarketEventsToday.ts`, then refactor `TodayEventCard` from a static placeholder into a tab-filtered list (전체 / 경제지표 / 실적).

**Tech Stack:** Python 3.13, SQLAlchemy 2.x async, Alembic, FastAPI, Pydantic v2, pytest + pytest-asyncio, httpx, React 18 + Vite + Vitest + React Testing Library.

**Task Branch:** `feature/ROB-132-economic-calendar-discover-events`
**PR base:** `main`
**Linear:** https://linear.app/mgh3326/issue/ROB-132

---

## File Structure

### New backend files
- `app/services/market_events/forexfactory_helpers.py` — per-day ForexFactory XML fetch + ET→UTC conversion. Reuses XML parsing logic from `app/services/external/forexfactory_calendar.py`.
- `alembic/versions/<rev>_add_market_events_currency.py` — adds `currency` column to `market_events`.
- `tests/services/test_market_events_forexfactory_helpers.py` — XML parsing + date filter tests.
- `tests/services/test_market_events_normalizers_forexfactory.py` — pure normalizer tests for ForexFactory rows. (Or extend existing `test_market_events_normalizers.py`.)

### Modified backend files
- `app/models/market_events.py` — add `currency: Mapped[str | None]` column on `MarketEvent`.
- `app/services/market_events/normalizers.py` — add `normalize_forexfactory_event_row(row)`.
- `app/services/market_events/ingestion.py` — add `ingest_economic_events_for_date(db, target_date, fetch_rows=...)`.
- `app/services/market_events/repository.py` — include `"currency"` in `update_columns`.
- `app/services/market_events/query_service.py` — pass `currency` from row to response.
- `app/schemas/market_events.py` — add `currency: str | None = None` to `MarketEventResponse`.
- `app/services/market_events/taxonomy.py` — add `"forexfactory"` to `SOURCES` (already has `economic` in `CATEGORIES` and `global` in `MARKETS`).
- `scripts/ingest_market_events.py` — extend `--source` / `--category` / `--market` choices and `SUPPORTED` map; emit JSON-ish summary line.
- `tests/services/test_market_events_ingestion.py` — add ingestion test for ForexFactory path with injected fetcher.
- `tests/services/test_market_events_query_service.py` — add a test for `category=economic` filtering and currency surfacing.
- `tests/test_market_events_router.py` — add test for `category=economic` filter pass-through.
- `tests/test_market_events_cli.py` — add tests for new `(forexfactory, economic, global)` combo + dry-run.

### New frontend files
- `frontend/invest/src/types/marketEvents.ts` — TypeScript types mirroring `MarketEventResponse` / `MarketEventsDayResponse`.
- `frontend/invest/src/api/marketEvents.ts` — `fetchMarketEventsToday(params, signal)`.
- `frontend/invest/src/hooks/useMarketEventsToday.ts` — fetch + state hook (loading / error / ready).
- `frontend/invest/src/__tests__/marketEvents.api.test.ts` — API-client tests.
- `frontend/invest/src/__tests__/TodayEventCard.test.tsx` — Card behavior + tab filter tests.

### Modified frontend files
- `frontend/invest/src/components/discover/TodayEventCard.tsx` — replace static body with tabbed event list.
- `frontend/invest/src/pages/DiscoverPage.tsx` — pass an injectable `eventsState` prop (mirrors `state` pattern) so the existing test stays stable.
- `frontend/invest/src/__tests__/DiscoverPage.test.tsx` — pass mocked `eventsState` to keep test deterministic.

### Documentation
- `docs/runbooks/market-events-ingestion.md` — append a "Economic events (ForexFactory, ROB-132)" section.

---

## Pre-flight

- [ ] **Step 0.1: Confirm clean working tree on a fresh branch**

```bash
cd ~/auto_trader && git switch main && git pull
git worktree add ~/auto_trader/.worktrees/ROB-132 -b feature/ROB-132-economic-calendar-discover-events main
cd ~/auto_trader/.worktrees/ROB-132
git status
```

Expected: `On branch feature/ROB-132-economic-calendar-discover-events`, nothing to commit.

- [ ] **Step 0.2: Verify dev environment**

```bash
uv sync --all-groups
docker compose up -d postgres redis
uv run alembic current
```

Expected: shows the latest revision (currently includes `a7e9c128` market events foundation).

- [ ] **Step 0.3: Sanity-run the existing market-events tests baseline**

```bash
uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q
```

Expected: all passing (this is the ROB-128 baseline).

---

## Task 1: Add `currency` column to `market_events`

**Files:**
- Modify: `app/models/market_events.py:75-78` (insert after `country`)
- Create: `alembic/versions/<rev>_add_market_events_currency.py`

- [ ] **Step 1.1: Add `currency` to the SQLAlchemy model**

Edit `app/models/market_events.py`. After the `country` column on `MarketEvent` (line 74), add:

```python
    currency: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 1.2: Generate the Alembic migration**

```bash
uv run alembic revision --autogenerate -m "add currency column to market_events"
```

Expected: a new file under `alembic/versions/` is printed. Open it.

- [ ] **Step 1.3: Verify the generated migration adds exactly the currency column**

The migration file should contain only:

```python
def upgrade() -> None:
    op.add_column(
        "market_events",
        sa.Column("currency", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_events", "currency")
```

If autogenerate added unrelated diffs (e.g., index renames), trim them — this PR only adds `currency`.

- [ ] **Step 1.4: Apply the migration locally**

```bash
uv run alembic upgrade head
uv run alembic current
```

Expected: new revision is current.

- [ ] **Step 1.5: Roll back and re-apply once to verify downgrade**

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: both succeed without error.

- [ ] **Step 1.6: Commit**

```bash
git add app/models/market_events.py alembic/versions/*_add_market_events_currency.py
git commit -m "feat(market_events): add currency column for economic events"
```

---

## Task 2: Surface `currency` in repository + schema + query service

**Files:**
- Modify: `app/services/market_events/repository.py:43-60`
- Modify: `app/schemas/market_events.py:27-50`
- Modify: `app/services/market_events/query_service.py:104-141`

- [ ] **Step 2.1: Write failing test for currency round-trip via the query service**

Append to `tests/services/test_market_events_query_service.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_service_surfaces_currency_field(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "economic",
            "market": "global",
            "country": "US",
            "currency": "USD",
            "title": "US CPI",
            "event_date": date(2026, 5, 13),
            "status": "scheduled",
            "source": "forexfactory",
            "source_event_id": "ff::USD::CPI::2026-05-13T12:30:00Z",
        },
        [],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    response = await svc.list_for_date(date(2026, 5, 13))
    assert len(response.events) == 1
    assert response.events[0].currency == "USD"
    assert response.events[0].country == "US"
```

- [ ] **Step 2.2: Run the failing test**

```bash
uv run pytest tests/services/test_market_events_query_service.py::test_query_service_surfaces_currency_field -q
```

Expected: FAIL — `currency` is not in `MarketEventResponse` and not propagated through the repo.

- [ ] **Step 2.3: Add `currency` to the response schema**

Edit `app/schemas/market_events.py`. After the `country` field on `MarketEventResponse` (line 31), add:

```python
    currency: str | None = None
```

- [ ] **Step 2.4: Add `currency` to repository update columns**

Edit `app/services/market_events/repository.py`. In `upsert_event_with_values`, extend the `update_columns` tuple:

```python
        update_columns = {
            k: payload.get(k)
            for k in (
                "country",
                "currency",
                "company_name",
                "title",
                "release_time_utc",
                "release_time_local",
                "source_timezone",
                "time_hint",
                "importance",
                "status",
                "source_url",
                "raw_payload_json",
                "fetched_at",
            )
            if k in payload
        }
```

- [ ] **Step 2.5: Pass currency through `MarketEventsQueryService`**

Edit `app/services/market_events/query_service.py`. In the `MarketEventResponse(...)` construction, add:

```python
                    country=row.country,
                    currency=row.currency,
```

immediately after the existing `country=row.country` line.

- [ ] **Step 2.6: Re-run the test**

```bash
uv run pytest tests/services/test_market_events_query_service.py::test_query_service_surfaces_currency_field -q
```

Expected: PASS.

- [ ] **Step 2.7: Run the full market-events test suite to ensure no regression**

```bash
uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q
```

Expected: all PASS.

- [ ] **Step 2.8: Commit**

```bash
git add app/services/market_events/repository.py app/services/market_events/query_service.py \
        app/schemas/market_events.py tests/services/test_market_events_query_service.py
git commit -m "feat(market_events): propagate currency through repo, schema, query service"
```

---

## Task 3: Extend taxonomy with `forexfactory` source

**Files:**
- Modify: `app/services/market_events/taxonomy.py:34-36`
- Modify (or create): `tests/services/test_market_events_taxonomy.py` — add coverage

- [ ] **Step 3.1: Write failing test for the new source name**

Add to `tests/services/test_market_events_taxonomy.py`:

```python
@pytest.mark.unit
def test_sources_include_forexfactory():
    from app.services.market_events.taxonomy import SOURCES
    assert "forexfactory" in SOURCES
```

(`economic` and `global` are already in `CATEGORIES` and `MARKETS`; no change needed there.)

- [ ] **Step 3.2: Run failing test**

```bash
uv run pytest tests/services/test_market_events_taxonomy.py::test_sources_include_forexfactory -q
```

Expected: FAIL — `forexfactory` is not yet listed.

- [ ] **Step 3.3: Add `forexfactory` to SOURCES**

Edit `app/services/market_events/taxonomy.py:34-36`:

```python
SOURCES: frozenset[str] = frozenset(
    {"finnhub", "dart", "upbit", "bithumb", "binance", "token_unlocks", "forexfactory"}
)
```

- [ ] **Step 3.4: Re-run test**

Expected: PASS.

- [ ] **Step 3.5: Commit**

```bash
git add app/services/market_events/taxonomy.py tests/services/test_market_events_taxonomy.py
git commit -m "feat(market_events): allow forexfactory as a source"
```

---

## Task 4: Pure normalizer for ForexFactory rows

**Files:**
- Create or extend: `tests/services/test_market_events_normalizers.py` — new tests at the bottom
- Modify: `app/services/market_events/normalizers.py` — append new function

We design the normalizer to take a uniform row dict shaped like:

```python
{
    "title": "Core CPI m/m",
    "currency": "USD",
    "country": "US",
    "event_date": date(2026, 5, 13),  # ET-day
    "release_time_utc": datetime(2026, 5, 13, 12, 30, tzinfo=UTC),  # may be None for "all day"/"tentative"
    "release_time_local": datetime(2026, 5, 13, 8, 30),  # ET wall-clock
    "time_hint_raw": "8:30am",  # original time string
    "impact": "high",  # low | medium | high | holiday
    "actual": "0.3%",
    "forecast": "0.3%",
    "previous": "0.4%",
    "source_event_id": "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z",  # stable derived key
}
```

- [ ] **Step 4.1: Write failing tests**

Append to `tests/services/test_market_events_normalizers.py`:

```python
from datetime import datetime, UTC


FF_ROW_HIGH_IMPACT = {
    "title": "Core CPI m/m",
    "currency": "USD",
    "country": "US",
    "event_date": date(2026, 5, 13),
    "release_time_utc": datetime(2026, 5, 13, 12, 30, tzinfo=UTC),
    "release_time_local": datetime(2026, 5, 13, 8, 30),
    "time_hint_raw": "8:30am",
    "impact": "high",
    "actual": "0.3%",
    "forecast": "0.3%",
    "previous": "0.4%",
    "source_event_id": "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z",
}


@pytest.mark.unit
def test_normalize_forexfactory_high_impact_event():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    event, values = normalize_forexfactory_event_row(FF_ROW_HIGH_IMPACT)
    assert event["category"] == "economic"
    assert event["market"] == "global"
    assert event["country"] == "US"
    assert event["currency"] == "USD"
    assert event["title"] == "Core CPI m/m"
    assert event["event_date"] == date(2026, 5, 13)
    assert event["release_time_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert event["source_timezone"] == "America/New_York"
    assert event["importance"] == 3
    assert event["status"] == "released"  # actual is present
    assert event["source"] == "forexfactory"
    assert event["source_event_id"] == "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z"

    by_metric = {v["metric_name"]: v for v in values}
    assert by_metric["actual"]["actual"] is not None
    assert by_metric["actual"]["forecast"] is not None
    assert by_metric["actual"]["previous"] is not None
    assert by_metric["actual"]["unit"] == "%"


@pytest.mark.unit
def test_normalize_forexfactory_scheduled_when_no_actual():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {**FF_ROW_HIGH_IMPACT, "actual": None}
    event, _ = normalize_forexfactory_event_row(row)
    assert event["status"] == "scheduled"


@pytest.mark.unit
def test_normalize_forexfactory_low_medium_high_to_int_importance():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    for raw, expected in [("low", 1), ("medium", 2), ("high", 3), ("holiday", None)]:
        row = {**FF_ROW_HIGH_IMPACT, "impact": raw}
        event, _ = normalize_forexfactory_event_row(row)
        assert event["importance"] == expected, raw


@pytest.mark.unit
def test_normalize_forexfactory_strips_value_suffixes_to_decimal():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {**FF_ROW_HIGH_IMPACT, "actual": "1.25%", "forecast": "1.30%", "previous": "1.10%"}
    _, values = normalize_forexfactory_event_row(row)
    by_metric = {v["metric_name"]: v for v in values}
    val = by_metric["actual"]
    assert val["actual"] == Decimal("1.25")
    assert val["forecast"] == Decimal("1.30")
    assert val["previous"] == Decimal("1.10")
    assert val["unit"] == "%"


@pytest.mark.unit
def test_normalize_forexfactory_emits_no_values_when_all_blank():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    row = {**FF_ROW_HIGH_IMPACT, "actual": None, "forecast": None, "previous": None}
    _, values = normalize_forexfactory_event_row(row)
    assert values == []


@pytest.mark.unit
def test_normalize_forexfactory_requires_title_and_date():
    from app.services.market_events.normalizers import normalize_forexfactory_event_row

    bad = {**FF_ROW_HIGH_IMPACT, "title": ""}
    with pytest.raises(ValueError):
        normalize_forexfactory_event_row(bad)
```

- [ ] **Step 4.2: Run failing tests**

```bash
uv run pytest tests/services/test_market_events_normalizers.py -k forexfactory -q
```

Expected: FAIL — function does not exist.

- [ ] **Step 4.3: Implement `normalize_forexfactory_event_row`**

Append to `app/services/market_events/normalizers.py`:

```python
_FF_IMPORTANCE_MAP = {"low": 1, "medium": 2, "high": 3}


def _strip_unit_and_decimal(value: Any) -> tuple[Decimal | None, str | None]:
    """Return (Decimal, unit) parsed from strings like '1.25%', '50K', '2.4'.

    Returns (None, None) for empty/None inputs.
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if not s:
        return None, None
    unit = None
    if s.endswith("%"):
        unit = "%"
        s = s[:-1].strip()
    elif s.endswith(("K", "M", "B", "T")):
        unit = s[-1]
        s = s[:-1].strip()
    try:
        return Decimal(s), unit
    except Exception:
        return None, unit


def normalize_forexfactory_event_row(
    row: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize one ForexFactory event row into MarketEvent + values dicts.

    The row shape is produced by `app/services/market_events/forexfactory_helpers.py`.
    """
    title = (row.get("title") or "").strip()
    event_date = row.get("event_date")
    if not title or event_date is None:
        raise ValueError("forexfactory row missing title or event_date")

    impact = (row.get("impact") or "").strip().lower()
    importance = _FF_IMPORTANCE_MAP.get(impact)

    actual_raw = row.get("actual")
    forecast_raw = row.get("forecast")
    previous_raw = row.get("previous")
    status = "released" if actual_raw not in (None, "") else "scheduled"

    event = {
        "category": "economic",
        "market": "global",
        "country": row.get("country"),
        "currency": row.get("currency"),
        "symbol": None,
        "company_name": None,
        "title": title,
        "event_date": event_date,
        "release_time_utc": row.get("release_time_utc"),
        "release_time_local": row.get("release_time_local"),
        "source_timezone": "America/New_York",
        "time_hint": row.get("time_hint_raw") or "unknown",
        "importance": importance,
        "status": status,
        "source": "forexfactory",
        "source_event_id": row.get("source_event_id"),
        "source_url": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "raw_payload_json": dict(row),
    }

    actual_dec, actual_unit = _strip_unit_and_decimal(actual_raw)
    forecast_dec, forecast_unit = _strip_unit_and_decimal(forecast_raw)
    previous_dec, previous_unit = _strip_unit_and_decimal(previous_raw)
    unit = actual_unit or forecast_unit or previous_unit

    values: list[dict[str, Any]] = []
    if any(v is not None for v in (actual_dec, forecast_dec, previous_dec)):
        values.append(
            {
                "metric_name": "actual",
                "period": event_date.isoformat(),
                "actual": actual_dec,
                "forecast": forecast_dec,
                "previous": previous_dec,
                "unit": unit,
            }
        )

    return event, values
```

Note: `raw_payload_json` will need datetime fields JSON-serializable. The existing `_redact_sensitive_keys` only redacts keys; non-serializable values would fail at write time. We mitigate in Task 5 by re-stringifying datetimes in the fetcher row before normalization (the values stored under `release_time_utc` / `release_time_local` are duplicated as ISO strings in the raw row).

- [ ] **Step 4.4: Re-run tests**

```bash
uv run pytest tests/services/test_market_events_normalizers.py -k forexfactory -q
```

Expected: all PASS.

- [ ] **Step 4.5: Commit**

```bash
git add app/services/market_events/normalizers.py tests/services/test_market_events_normalizers.py
git commit -m "feat(market_events): add ForexFactory event normalizer for economic category"
```

---

## Task 5: ForexFactory per-day fetch helper

**Files:**
- Create: `app/services/market_events/forexfactory_helpers.py`
- Create: `tests/services/test_market_events_forexfactory_helpers.py`

The helper must:
- Fetch both this-week and next-week XML once and parse to a list of rows.
- Return only rows whose ET event_date == `target_date`.
- Convert each ET wall-clock time to UTC (using `zoneinfo.ZoneInfo("America/New_York")`).
- Compute a stable `source_event_id`: `f"ff::{currency}::{title}::{utc_iso}"` (or with `event_date` when no time).

- [ ] **Step 5.1: Write failing tests with sample XML**

Create `tests/services/test_market_events_forexfactory_helpers.py`:

```python
"""ForexFactory per-day fetch helper tests (ROB-132)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<weeklyevents>
  <event>
    <title>Core CPI m/m</title>
    <country>USD</country>
    <date>05-13-2026</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>0.3%</forecast>
    <previous>0.4%</previous>
    <actual>0.3%</actual>
  </event>
  <event>
    <title>Trade Balance</title>
    <country>EUR</country>
    <date>05-13-2026</date>
    <time>5:00am</time>
    <impact>Low</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
  <event>
    <title>Bank Holiday</title>
    <country>JPY</country>
    <date>05-14-2026</date>
    <time>All Day</time>
    <impact>Holiday</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
</weeklyevents>
"""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_for_date_filters_to_target_day():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    assert len(rows) == 2
    assert {r["title"] for r in rows} == {"Core CPI m/m", "Trade Balance"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_converts_et_to_utc_and_emits_stable_id():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    cpi = next(r for r in rows if r["title"] == "Core CPI m/m")
    # 8:30am ET in May (EDT, UTC-4) -> 12:30 UTC.
    assert cpi["release_time_utc"] == datetime(2026, 5, 13, 12, 30, tzinfo=UTC)
    assert cpi["currency"] == "USD"
    assert cpi["impact"] == "high"
    assert cpi["source_event_id"].startswith("ff::USD::Core CPI m/m::")
    assert "2026-05-13T12:30" in cpi["source_event_id"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_handles_all_day_no_time():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=[SAMPLE_XML])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 14))

    assert len(rows) == 1
    holiday = rows[0]
    assert holiday["release_time_utc"] is None
    assert holiday["release_time_local"] is None
    assert holiday["time_hint_raw"].lower() in ("all day", "all_day", "tentative", "")
    assert holiday["source_event_id"].startswith("ff::JPY::Bank Holiday::2026-05-14")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_forexfactory_returns_empty_on_xml_error():
    from app.services.market_events import forexfactory_helpers as ff

    with patch.object(ff, "_fetch_xml_documents", AsyncMock(return_value=["not-xml"])):
        rows = await ff.fetch_forexfactory_events_for_date(date(2026, 5, 13))

    assert rows == []
```

- [ ] **Step 5.2: Run failing tests**

```bash
uv run pytest tests/services/test_market_events_forexfactory_helpers.py -q
```

Expected: FAIL — module does not exist.

- [ ] **Step 5.3: Implement the helper**

Create `app/services/market_events/forexfactory_helpers.py`:

```python
"""Per-day ForexFactory economic-calendar fetch helper (ROB-132).

Fetches `ff_calendar_thisweek.xml` and (when needed) `ff_calendar_nextweek.xml`,
parses each event into a uniform dict, converts ET wall-clock times to UTC, and
filters to rows whose ET-day matches the requested target_date.

Caller passes the resulting rows into
`app.services.market_events.normalizers.normalize_forexfactory_event_row`.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"
ET_TZ = ZoneInfo("America/New_York")


async def _fetch_xml_documents(target_date: date) -> list[str]:
    """Fetch this-week, plus next-week if target_date is within 7 days of next Monday.

    Kept as a module-level seam for tests to patch.
    """
    urls = [THISWEEK_URL]
    today = datetime.now(UTC).date()
    if (target_date - today).days >= 5:
        urls.append(NEXTWEEK_URL)
    documents: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in urls:
            response = await client.get(url)
            response.raise_for_status()
            documents.append(response.text)
    return documents


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%m-%d-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_et_time(time_str: str | None) -> tuple[int, int] | None:
    if not time_str:
        return None
    cleaned = time_str.strip().lower()
    if cleaned in ("", "all day", "tentative"):
        return None
    try:
        parsed = datetime.strptime(cleaned, "%I:%M%p")
    except ValueError:
        return None
    return parsed.hour, parsed.minute


def _parse_one_xml(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("forexfactory XML parse error: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for elem in root.findall("event"):
        title = (elem.findtext("title") or "").strip()
        currency = (elem.findtext("country") or "").strip()
        date_str = elem.findtext("date") or ""
        time_str = elem.findtext("time") or ""
        impact = (elem.findtext("impact") or "").strip().lower()
        forecast = elem.findtext("forecast") or ""
        previous = elem.findtext("previous") or ""
        actual = elem.findtext("actual") or ""

        event_date = _parse_date(date_str)
        if event_date is None:
            continue

        hm = _parse_et_time(time_str)
        if hm is None:
            release_local = None
            release_utc = None
            id_iso = event_date.isoformat()
        else:
            hour, minute = hm
            release_local = datetime(
                event_date.year,
                event_date.month,
                event_date.day,
                hour,
                minute,
            )
            release_utc = release_local.replace(tzinfo=ET_TZ).astimezone(UTC)
            id_iso = release_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        source_event_id = f"ff::{currency}::{title}::{id_iso}"

        rows.append(
            {
                "title": title,
                "currency": currency or None,
                "country": currency or None,  # FX rows describe the affected currency
                "event_date": event_date,
                "release_time_utc": release_utc,
                "release_time_local": release_local,
                "time_hint_raw": time_str,
                "impact": impact,
                "actual": actual or None,
                "forecast": forecast or None,
                "previous": previous or None,
                "source_event_id": source_event_id,
            }
        )
    return rows


async def fetch_forexfactory_events_for_date(
    target_date: date,
) -> list[dict[str, Any]]:
    """Return ForexFactory rows whose ET-day == target_date.

    Returns [] on fetch/parse error to allow per-day partition retries to record
    the failure higher up the stack rather than crashing the whole run.
    """
    try:
        documents = await _fetch_xml_documents(target_date)
    except Exception as exc:  # network errors bubble up to ingestion
        logger.warning("forexfactory fetch failed for %s: %s", target_date, exc)
        raise

    rows: list[dict[str, Any]] = []
    for xml_text in documents:
        rows.extend(_parse_one_xml(xml_text))

    return [r for r in rows if r["event_date"] == target_date]
```

Note: serialization. `release_time_utc` / `release_time_local` are `datetime` objects in the row, but they will end up in `raw_payload_json` via the normalizer. The repo writes `raw_payload_json` through `_redact_sensitive_keys`, which preserves keys but does not serialize datetimes. To stay safe, the JSON DB type accepts native dicts and SQLAlchemy converts datetimes via the JSONB adapter; if the existing repo write fails on datetime, we add ISO-string aliases — see Task 6.

- [ ] **Step 5.4: Re-run tests**

```bash
uv run pytest tests/services/test_market_events_forexfactory_helpers.py -q
```

Expected: all PASS.

- [ ] **Step 5.5: Commit**

```bash
git add app/services/market_events/forexfactory_helpers.py tests/services/test_market_events_forexfactory_helpers.py
git commit -m "feat(market_events): add per-day ForexFactory fetch helper with ET->UTC conversion"
```

---

## Task 6: Ingestion orchestrator for economic events

**Files:**
- Modify: `app/services/market_events/ingestion.py:1-29` (imports), and append `ingest_economic_events_for_date`
- Modify: `tests/services/test_market_events_ingestion.py` — append tests
- Modify: `app/services/market_events/normalizers.py` (small) — strip non-JSON-serializable values before storing in `raw_payload_json`

- [ ] **Step 6.1: Write failing ingestion test**

Append to `tests/services/test_market_events_ingestion.py`:

```python
from datetime import UTC, datetime as _dt


FF_ROW = {
    "title": "Core CPI m/m",
    "currency": "USD",
    "country": "USD",
    "event_date": date(2026, 5, 13),
    "release_time_utc": _dt(2026, 5, 13, 12, 30, tzinfo=UTC),
    "release_time_local": _dt(2026, 5, 13, 8, 30),
    "time_hint_raw": "8:30am",
    "impact": "high",
    "actual": "0.3%",
    "forecast": "0.3%",
    "previous": "0.4%",
    "source_event_id": "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z",
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_for_date_succeeds(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        assert d == date(2026, 5, 13)
        return [FF_ROW]

    result = await ingestion.ingest_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake_fetch
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.category == "economic"
    assert e.market == "global"
    assert e.source == "forexfactory"
    assert e.currency == "USD"
    assert e.importance == 3

    values = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(values) == 1
    assert values[0].metric_name == "actual"
    assert values[0].unit == "%"

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].source == "forexfactory"
    assert parts[0].category == "economic"
    assert parts[0].market == "global"
    assert parts[0].status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_is_idempotent(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        return [FF_ROW]

    for _ in range(2):
        await ingestion.ingest_economic_events_for_date(
            db_session, date(2026, 5, 13), fetch_rows=fake_fetch
        )
        await db_session.commit()

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_records_failure(db_session):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    async def boom(d):
        raise TimeoutError("fetch timed out")

    result = await ingestion.ingest_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=boom
    )
    await db_session.commit()

    assert result.status == "failed"
    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1
```

- [ ] **Step 6.2: Run failing tests**

```bash
uv run pytest tests/services/test_market_events_ingestion.py -k economic -q
```

Expected: FAIL — `ingest_economic_events_for_date` does not exist.

- [ ] **Step 6.3: Make `raw_payload_json` JSON-safe in the normalizer**

Datetimes inside the row dict will fail JSONB serialization. Inside `normalize_forexfactory_event_row`, replace the `raw_payload_json` line:

```python
        "raw_payload_json": _row_to_jsonable(row),
```

…and add a helper near the top of `normalizers.py`:

```python
def _row_to_jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Strip / stringify values inside `row` so the dict is JSONB-serializable.

    Datetimes -> ISO strings; date -> ISO string; Decimals -> str.
    """
    import datetime as _dt
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, _dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, _dt.date):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out
```

- [ ] **Step 6.4: Implement `ingest_economic_events_for_date`**

In `app/services/market_events/ingestion.py`, append:

```python
async def ingest_economic_events_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest ForexFactory economic-calendar events for one day.

    `fetch_rows` is an optional injection point. Default uses
    `app.services.market_events.forexfactory_helpers.fetch_forexfactory_events_for_date`.
    """
    if fetch_rows is None:
        from app.services.market_events.forexfactory_helpers import (
            fetch_forexfactory_events_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "forexfactory"
    category = "economic"
    market = "global"
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    try:
        from app.services.market_events.normalizers import (
            normalize_forexfactory_event_row,
        )

        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_forexfactory_event_row(row)
            except ValueError as exc:
                logger.warning("skipping unparseable forexfactory row: %s (%s)", row, exc)
                continue
            await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        await repo.mark_partition_succeeded(partition, event_count=upserted)
        return IngestionRunResult(
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            status="succeeded",
            event_count=upserted,
        )
    except Exception as exc:
        logger.exception("forexfactory ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=exc,
        )
```

- [ ] **Step 6.5: Re-run ingestion tests**

```bash
uv run pytest tests/services/test_market_events_ingestion.py -q
```

Expected: ALL PASS (existing finnhub/dart tests stay green).

- [ ] **Step 6.6: Commit**

```bash
git add app/services/market_events/ingestion.py app/services/market_events/normalizers.py \
        tests/services/test_market_events_ingestion.py
git commit -m "feat(market_events): add ForexFactory economic-event ingestion orchestrator"
```

---

## Task 7: CLI extension (`forexfactory / economic / global`)

**Files:**
- Modify: `scripts/ingest_market_events.py`
- Modify: `tests/test_market_events_cli.py`

- [ ] **Step 7.1: Write failing CLI tests**

Append to `tests/test_market_events_cli.py`:

```python
@pytest.mark.unit
def test_parse_args_accepts_forexfactory_economic_global():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        [
            "--source",
            "forexfactory",
            "--category",
            "economic",
            "--market",
            "global",
            "--from-date",
            "2026-05-13",
            "--to-date",
            "2026-05-13",
            "--dry-run",
        ]
    )
    assert ns.source == "forexfactory"
    assert ns.category == "economic"
    assert ns.market == "global"
    assert ns.dry_run is True


@pytest.mark.unit
def test_parse_args_rejects_forexfactory_with_us_market():
    import argparse

    from scripts.ingest_market_events import parse_args

    with pytest.raises((SystemExit, argparse.ArgumentTypeError, ValueError)):
        parse_args(
            [
                "--source",
                "forexfactory",
                "--category",
                "economic",
                "--market",
                "us",
                "--from-date",
                "2026-05-13",
                "--to-date",
                "2026-05-13",
            ]
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_dry_run_does_not_call_orchestrator(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake = AsyncMock()
    monkeypatch.setitem(cli.SUPPORTED, ("forexfactory", "economic", "global"), fake)

    rc = await cli.run_ingest(
        db=db_session,
        source="forexfactory",
        category="economic",
        market="global",
        from_date=date(2026, 5, 13),
        to_date=date(2026, 5, 13),
        dry_run=True,
    )
    assert rc == 0
    fake.assert_not_awaited()
```

- [ ] **Step 7.2: Run failing tests**

```bash
uv run pytest tests/test_market_events_cli.py -q
```

Expected: FAILs on the new tests.

- [ ] **Step 7.3: Extend CLI choices and SUPPORTED**

In `scripts/ingest_market_events.py`:

```python
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_us_earnings_for_date,
)


SUPPORTED = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
    ("forexfactory", "economic", "global"): ingest_economic_events_for_date,
}
```

In `parse_args`:

```python
    parser.add_argument(
        "--source",
        default="finnhub",
        choices=["finnhub", "dart", "forexfactory"],
    )
    parser.add_argument(
        "--category",
        default="earnings",
        choices=["earnings", "disclosure", "economic"],
    )
    parser.add_argument(
        "--market",
        default="us",
        choices=["us", "kr", "global"],
    )
```

The existing `key not in SUPPORTED` check then naturally rejects (`forexfactory`, `economic`, `us`) etc.

- [ ] **Step 7.4: Re-run CLI tests**

```bash
uv run pytest tests/test_market_events_cli.py -q
```

Expected: ALL PASS.

- [ ] **Step 7.5: Add JSON-ish summary line**

Replace the trailing `logger.info("ingest complete: ...")` line in `run_ingest` with a structured summary line plus existing log:

```python
    summary = {
        "source": source,
        "category": category,
        "market": market,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dry_run": dry_run,
        "succeeded": succeeded,
        "failed": failed,
    }
    import json as _json
    print(_json.dumps(summary))
    logger.info("ingest complete: %s", summary)
    return 0 if failed == 0 else 2
```

- [ ] **Step 7.6: Live dry-run smoke (no DB writes)**

```bash
uv run python -m scripts.ingest_market_events \
  --source forexfactory --category economic --market global \
  --from-date 2026-05-13 --to-date 2026-05-13 --dry-run
```

Expected: prints a one-line JSON summary like `{"source": "forexfactory", ..., "dry_run": true, "succeeded": 1, "failed": 0}` and exits 0. Verify with `psql` (or any read tool) that **no** new rows landed in `market_events` / `market_event_ingestion_partitions`.

- [ ] **Step 7.7: Commit**

```bash
git add scripts/ingest_market_events.py tests/test_market_events_cli.py
git commit -m "feat(market_events): extend CLI for forexfactory/economic/global with JSON summary"
```

---

## Task 8: Router + query service filter pass-through for `category=economic`

**Files:**
- Modify: `tests/test_market_events_router.py` — add tests
- Modify: `tests/services/test_market_events_query_service.py` — add tests

The router already accepts `category` and `market` query params and forwards to the query service, which validates against `taxonomy.CATEGORIES` / `MARKETS`. We add coverage that confirms `economic` and `global` flow through without 400 and that forexfactory rows surface.

- [ ] **Step 8.1: Add query-service test for economic filter**

Append to `tests/services/test_market_events_query_service.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_service_filters_economic_events(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "economic",
            "market": "global",
            "currency": "USD",
            "country": "USD",
            "title": "US CPI",
            "event_date": date(2026, 5, 13),
            "status": "released",
            "source": "forexfactory",
            "source_event_id": "ff::USD::US CPI::2026-05-13T12:30:00Z",
        },
        [],
    )
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "IONQ",
            "event_date": date(2026, 5, 13),
            "status": "released",
            "source": "finnhub",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    only_econ = await svc.list_for_date(date(2026, 5, 13), category="economic")
    assert len(only_econ.events) == 1
    assert only_econ.events[0].source == "forexfactory"
    assert only_econ.events[0].currency == "USD"
```

- [ ] **Step 8.2: Add router smoke test for economic category**

Append to `tests/test_market_events_router.py`:

```python
@pytest.mark.integration
def test_get_today_events_filters_by_category_economic(db_session):
    """Smoke test: passing category=economic does not 400 and filters correctly."""
    with TestClient(_app()) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-13&category=economic&market=global",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-05-13"
        assert isinstance(body["events"], list)


@pytest.mark.integration
def test_get_today_events_rejects_unknown_category(db_session):
    with TestClient(_app()) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-13&category=bogus",
        )
        assert response.status_code == 400
```

- [ ] **Step 8.3: Run the new tests (they should already pass — taxonomy already has `economic` / `global`)**

```bash
uv run pytest tests/services/test_market_events_query_service.py tests/test_market_events_router.py -q
```

Expected: ALL PASS. (No code change needed beyond Task 2, 3, 4, 6.)

- [ ] **Step 8.4: Commit**

```bash
git add tests/services/test_market_events_query_service.py tests/test_market_events_router.py
git commit -m "test(market_events): cover category=economic filter pass-through"
```

---

## Task 9: Frontend types + API client

**Files:**
- Create: `frontend/invest/src/types/marketEvents.ts`
- Create: `frontend/invest/src/api/marketEvents.ts`
- Create: `frontend/invest/src/__tests__/marketEvents.api.test.ts`

- [ ] **Step 9.1: Write failing API-client tests**

Create `frontend/invest/src/__tests__/marketEvents.api.test.ts`:

```typescript
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchMarketEventsToday } from "../api/marketEvents";
import type { MarketEventsDayResponse } from "../types/marketEvents";

const baseResponse: MarketEventsDayResponse = {
  date: "2026-05-13",
  events: [],
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("fetchMarketEventsToday hits the today endpoint with credentials", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  const result = await fetchMarketEventsToday();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/trading/api/market-events/today");
  expect(init).toMatchObject({ credentials: "include" });
  expect(result).toEqual(baseResponse);
});

test("fetchMarketEventsToday forwards category/market filters", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => baseResponse });

  await fetchMarketEventsToday({ category: "economic", market: "global" });

  const [url] = fetchMock.mock.calls[0] as [string];
  expect(url).toBe(
    "/trading/api/market-events/today?category=economic&market=global",
  );
});

test("fetchMarketEventsToday throws on non-ok response", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status: 500,
    json: async () => ({}),
  });
  await expect(fetchMarketEventsToday()).rejects.toThrow(/500/);
});
```

- [ ] **Step 9.2: Run failing tests**

```bash
cd frontend/invest && npm test -- --run marketEvents.api
```

Expected: FAIL — module not found.

- [ ] **Step 9.3: Add the type module**

Create `frontend/invest/src/types/marketEvents.ts`:

```typescript
export type MarketEventCategory =
  | "earnings"
  | "economic"
  | "disclosure"
  | "crypto_exchange_notice"
  | "crypto_protocol"
  | "tokenomics"
  | "regulatory";

export type MarketEventMarket = "us" | "kr" | "crypto" | "global";

export interface MarketEventValue {
  metric_name: string;
  period: string | null;
  actual: string | null;
  forecast: string | null;
  previous: string | null;
  revised_previous: string | null;
  unit: string | null;
  surprise: string | null;
  surprise_pct: string | null;
  released_at: string | null;
}

export interface MarketEvent {
  category: MarketEventCategory;
  market: MarketEventMarket;
  country: string | null;
  currency: string | null;
  symbol: string | null;
  company_name: string | null;
  title: string | null;
  event_date: string;
  release_time_utc: string | null;
  time_hint: string | null;
  importance: number | null;
  status: string;
  source: string;
  source_event_id: string | null;
  source_url: string | null;
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  held: boolean | null;
  watched: boolean | null;
  values: MarketEventValue[];
}

export interface MarketEventsDayResponse {
  date: string;
  events: MarketEvent[];
}

export interface FetchMarketEventsTodayParams {
  category?: MarketEventCategory;
  market?: MarketEventMarket;
  source?: string;
  /** ISO date — when omitted the backend defaults to today (server clock). */
  onDate?: string;
}
```

- [ ] **Step 9.4: Add the API client**

Create `frontend/invest/src/api/marketEvents.ts`:

```typescript
import type {
  FetchMarketEventsTodayParams,
  MarketEventsDayResponse,
} from "../types/marketEvents";

export async function fetchMarketEventsToday(
  params: FetchMarketEventsTodayParams = {},
  signal?: AbortSignal,
): Promise<MarketEventsDayResponse> {
  const search = new URLSearchParams();
  if (params.category) search.set("category", params.category);
  if (params.market) search.set("market", params.market);
  if (params.source) search.set("source", params.source);
  if (params.onDate) search.set("on_date", params.onDate);
  const qs = search.toString();
  const url = qs
    ? `/trading/api/market-events/today?${qs}`
    : "/trading/api/market-events/today";

  const res = await fetch(url, {
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new Error(`/trading/api/market-events/today ${res.status}`);
  }
  return (await res.json()) as MarketEventsDayResponse;
}
```

- [ ] **Step 9.5: Re-run API tests**

```bash
cd frontend/invest && npm test -- --run marketEvents.api
```

Expected: ALL PASS.

- [ ] **Step 9.6: Commit**

```bash
git add frontend/invest/src/types/marketEvents.ts \
        frontend/invest/src/api/marketEvents.ts \
        frontend/invest/src/__tests__/marketEvents.api.test.ts
git commit -m "feat(invest): add market-events API client + types"
```

---

## Task 10: Frontend hook `useMarketEventsToday`

**Files:**
- Create: `frontend/invest/src/hooks/useMarketEventsToday.ts`

This mirrors `useNewsIssues` so test patterns stay consistent.

- [ ] **Step 10.1: Add the hook**

Create `frontend/invest/src/hooks/useMarketEventsToday.ts`:

```typescript
import { useEffect, useMemo, useState } from "react";
import {
  fetchMarketEventsToday,
} from "../api/marketEvents";
import type {
  FetchMarketEventsTodayParams,
  MarketEventsDayResponse,
} from "../types/marketEvents";

export type MarketEventsTodayState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: MarketEventsDayResponse };

export interface UseMarketEventsTodayOptions {
  enabled?: boolean;
}

export function useMarketEventsToday(
  params: FetchMarketEventsTodayParams = {},
  options: UseMarketEventsTodayOptions = {},
) {
  const enabled = options.enabled ?? true;
  const [state, setState] = useState<MarketEventsTodayState>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const paramsKey = useMemo(() => JSON.stringify(params), [params]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchMarketEventsToday(params, controller.signal)
      .then((data) => setState({ status: "ready", data }))
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: e?.message ?? "failed" });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, paramsKey, tick]);

  return { state, reload: () => setTick((t) => t + 1) };
}
```

- [ ] **Step 10.2: Typecheck**

```bash
cd frontend/invest && npm run typecheck
```

Expected: PASS.

- [ ] **Step 10.3: Commit**

```bash
git add frontend/invest/src/hooks/useMarketEventsToday.ts
git commit -m "feat(invest): add useMarketEventsToday hook"
```

---

## Task 11: Refactor `TodayEventCard` to a tabbed event list

**Files:**
- Modify: `frontend/invest/src/components/discover/TodayEventCard.tsx`
- Create: `frontend/invest/src/__tests__/TodayEventCard.test.tsx`

Tabs:
- 전체 (no filter)
- 경제지표 (`category=economic`)
- 실적 (`category=earnings`)

Fetches **once** without a category filter, then filters client-side by tab. Loading / error / empty states render in Korean.

- [ ] **Step 11.1: Write failing component tests**

Create `frontend/invest/src/__tests__/TodayEventCard.test.tsx`:

```typescript
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { TodayEventCard } from "../components/discover/TodayEventCard";
import type {
  MarketEvent,
  MarketEventsDayResponse,
} from "../types/marketEvents";

function makeEvent(over: Partial<MarketEvent>): MarketEvent {
  return {
    category: "earnings",
    market: "us",
    country: null,
    currency: null,
    symbol: null,
    company_name: null,
    title: null,
    event_date: "2026-05-13",
    release_time_utc: null,
    time_hint: null,
    importance: null,
    status: "scheduled",
    source: "finnhub",
    source_event_id: null,
    source_url: null,
    fiscal_year: null,
    fiscal_quarter: null,
    held: null,
    watched: null,
    values: [],
    ...over,
  };
}

function makeResponse(events: MarketEvent[]): MarketEventsDayResponse {
  return { date: "2026-05-13", events };
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders loading state initially", () => {
  fetchMock.mockReturnValue(new Promise(() => {}));
  render(<TodayEventCard />);
  expect(screen.getByText(/불러오는 중/)).toBeInTheDocument();
});

test("renders empty state when there are no events", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => makeResponse([]) });
  render(<TodayEventCard />);
  expect(await screen.findByText(/오늘 표시할 이벤트가 없습니다/)).toBeInTheDocument();
});

test("renders error state when the fetch fails", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) });
  render(<TodayEventCard />);
  expect(await screen.findByText(/잠시 후 다시 시도해 주세요/)).toBeInTheDocument();
});

test("filters by tab — economic shows only economic rows", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse([
        makeEvent({
          category: "economic",
          market: "global",
          currency: "USD",
          country: "US",
          title: "US CPI",
          source: "forexfactory",
          importance: 3,
          values: [
            {
              metric_name: "actual",
              period: "2026-05-13",
              actual: "0.3",
              forecast: "0.3",
              previous: "0.4",
              revised_previous: null,
              unit: "%",
              surprise: null,
              surprise_pct: null,
              released_at: null,
            },
          ],
        }),
        makeEvent({
          category: "earnings",
          symbol: "IONQ",
          title: "IONQ earnings release",
        }),
      ]),
  });

  render(<TodayEventCard />);
  expect(await screen.findByText("US CPI")).toBeInTheDocument();
  expect(screen.getByText(/IONQ/)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.getByText("US CPI")).toBeInTheDocument();
  expect(screen.queryByText(/IONQ earnings release/)).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.queryByText("US CPI")).not.toBeInTheDocument();
  expect(screen.getByText(/IONQ earnings release/)).toBeInTheDocument();
});

test("renders forecast/previous/actual for economic events", async () => {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () =>
      makeResponse([
        makeEvent({
          category: "economic",
          market: "global",
          currency: "USD",
          title: "US CPI",
          source: "forexfactory",
          values: [
            {
              metric_name: "actual",
              period: "2026-05-13",
              actual: "0.3",
              forecast: "0.3",
              previous: "0.4",
              revised_previous: null,
              unit: "%",
              surprise: null,
              surprise_pct: null,
              released_at: null,
            },
          ],
        }),
      ]),
  });

  render(<TodayEventCard />);
  expect(await screen.findByText(/예상/)).toBeInTheDocument();
  expect(screen.getByText(/이전/)).toBeInTheDocument();
  expect(screen.getByText(/실제/)).toBeInTheDocument();
  expect(screen.getByText(/0\.3/)).toBeInTheDocument();
});
```

- [ ] **Step 11.2: Run failing tests**

```bash
cd frontend/invest && npm test -- --run TodayEventCard
```

Expected: FAIL — current `TodayEventCard` is static.

- [ ] **Step 11.3: Replace `TodayEventCard.tsx`**

Replace the entire file `frontend/invest/src/components/discover/TodayEventCard.tsx` with:

```tsx
// frontend/invest/src/components/discover/TodayEventCard.tsx
import { useMemo, useState } from "react";
import { useMarketEventsToday } from "../../hooks/useMarketEventsToday";
import type { MarketEvent } from "../../types/marketEvents";

type Tab = "all" | "economic" | "earnings";

const TAB_LABELS: Record<Tab, string> = {
  all: "전체",
  economic: "경제지표",
  earnings: "실적",
};

function formatLocalTime(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function EconomicRow({ event }: { event: MarketEvent }) {
  const value = event.values.find((v) => v.metric_name === "actual");
  const unit = value?.unit ?? "";
  const time = formatLocalTime(event.release_time_utc) || event.time_hint || "";
  return (
    <li
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: 13 }}>
          {event.currency ? `[${event.currency}] ` : ""}
          {event.title}
        </strong>
        <span className="subtle" style={{ fontSize: 12 }}>
          {time}
        </span>
      </div>
      {value && (
        <div className="subtle" style={{ fontSize: 12, display: "flex", gap: 12 }}>
          <span>예상 {value.forecast ?? "-"}{unit}</span>
          <span>이전 {value.previous ?? "-"}{unit}</span>
          <span>실제 {value.actual ?? "-"}{unit}</span>
        </div>
      )}
    </li>
  );
}

function EarningsRow({ event }: { event: MarketEvent }) {
  const eps = event.values.find((v) => v.metric_name === "eps");
  const time = formatLocalTime(event.release_time_utc) || event.time_hint || "";
  return (
    <li
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong style={{ fontSize: 13 }}>
          {event.symbol ?? ""} {event.title ?? ""}
        </strong>
        <span className="subtle" style={{ fontSize: 12 }}>
          {time}
        </span>
      </div>
      {eps && (
        <div className="subtle" style={{ fontSize: 12, display: "flex", gap: 12 }}>
          <span>EPS 예상 {eps.forecast ?? "-"}</span>
          <span>EPS 실제 {eps.actual ?? "-"}</span>
        </div>
      )}
    </li>
  );
}

function DisclosureRow({ event }: { event: MarketEvent }) {
  return (
    <li
      style={{
        padding: "8px 0",
        borderBottom: "1px solid var(--surface-2)",
        fontSize: 13,
      }}
    >
      <strong>{event.company_name ?? event.symbol ?? "공시"}</strong>{" "}
      <span className="subtle">{event.title ?? ""}</span>
    </li>
  );
}

function EventRow({ event }: { event: MarketEvent }) {
  if (event.category === "economic") return <EconomicRow event={event} />;
  if (event.category === "earnings") return <EarningsRow event={event} />;
  return <DisclosureRow event={event} />;
}

export function TodayEventCard() {
  const [tab, setTab] = useState<Tab>("all");
  const { state, reload } = useMarketEventsToday();

  const filtered = useMemo(() => {
    if (state.status !== "ready") return [];
    if (tab === "all") return state.data.events;
    return state.data.events.filter((e) => e.category === tab);
  }, [state, tab]);

  return (
    <section
      aria-labelledby="today-event-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <h2
        id="today-event-heading"
        style={{ margin: 0, fontSize: 14, fontWeight: 700 }}
      >
        오늘의 주요 이벤트
      </h2>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
          <button
            type="button"
            key={t}
            onClick={() => setTab(t)}
            aria-pressed={tab === t}
            style={{
              padding: "4px 10px",
              borderRadius: 999,
              border: "1px solid var(--surface-2)",
              background: tab === t ? "var(--surface-2)" : "transparent",
              fontSize: 12,
            }}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>
      {state.status === "loading" && (
        <div className="subtle" style={{ marginTop: 8 }}>
          불러오는 중…
        </div>
      )}
      {state.status === "error" && (
        <div style={{ marginTop: 8 }}>
          <div>잠시 후 다시 시도해 주세요.</div>
          <button type="button" onClick={reload}>
            재시도
          </button>
          <div className="subtle">{state.message}</div>
        </div>
      )}
      {state.status === "ready" && filtered.length === 0 && (
        <div className="subtle" style={{ marginTop: 8 }}>
          오늘 표시할 이벤트가 없습니다.
        </div>
      )}
      {state.status === "ready" && filtered.length > 0 && (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0 0",
          }}
        >
          {filtered.map((event) => (
            <EventRow
              key={
                event.source_event_id ??
                `${event.source}::${event.category}::${event.symbol ?? ""}::${event.event_date}::${event.title ?? ""}`
              }
              event={event}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
```

- [ ] **Step 11.4: Re-run component tests**

```bash
cd frontend/invest && npm test -- --run TodayEventCard
```

Expected: ALL PASS.

- [ ] **Step 11.5: Verify the existing Discover test still passes**

```bash
cd frontend/invest && npm test -- --run DiscoverPage
```

Expected: PASS — `DiscoverPage` imports `TodayEventCard` but the existing test does not assert on its body. The card mounts and uses `useMarketEventsToday`, which calls `fetch` — under the test's `vi.stubGlobal("fetch", ...)`-free environment, the fetch will hang in loading state. That's fine: existing assertions look at issue-card titles, not the event card body. **If** the test fails with an unhandled rejection, add a `vi.stubGlobal("fetch", () => new Promise(() => {}))` in the existing `DiscoverPage.test.tsx` before each test (use `beforeEach`).

- [ ] **Step 11.6: If the existing DiscoverPage test fails, fix it**

Edit `frontend/invest/src/__tests__/DiscoverPage.test.tsx`. At the top, add:

```typescript
import { afterEach, beforeEach, expect, test, vi } from "vitest";
```

…and add:

```typescript
beforeEach(() => {
  vi.stubGlobal("fetch", () => new Promise(() => {}));
});
afterEach(() => {
  vi.unstubAllGlobals();
});
```

Re-run:

```bash
cd frontend/invest && npm test -- --run DiscoverPage
```

Expected: PASS.

- [ ] **Step 11.7: Build to confirm bundle compiles**

```bash
cd frontend/invest && npm run build
```

Expected: Vite finishes without errors.

- [ ] **Step 11.8: Commit**

```bash
git add frontend/invest/src/components/discover/TodayEventCard.tsx \
        frontend/invest/src/__tests__/TodayEventCard.test.tsx \
        frontend/invest/src/__tests__/DiscoverPage.test.tsx
git commit -m "feat(invest): wire Discover TodayEventCard to market-events API with tabs"
```

---

## Task 12: Runbook update

**Files:**
- Modify: `docs/runbooks/market-events-ingestion.md`

- [ ] **Step 12.1: Append economic-events section**

Append the following at the end of the file, before "Handoff":

```markdown
## Economic events (ForexFactory, ROB-132)

ForexFactory weekly XML feeds are parsed per day and ingested as
`(source=forexfactory, category=economic, market=global)` rows.

### CLI

```bash
uv run python -m scripts.ingest_market_events \
  --source forexfactory --category economic --market global \
  --from-date 2026-05-13 --to-date 2026-05-13 --dry-run
```

### Idempotency

`source_event_id` is derived as `f"ff::{currency}::{title}::{utc_iso_or_date}"` so
repeated ingestion of the same release upserts on `(source, category, market,
source_event_id)`.

### UI

`/invest/app` Discover `TodayEventCard` consumes
`GET /trading/api/market-events/today` and filters client-side by `category`
into 전체 / 경제지표 / 실적 tabs.

### Open follow-ups specific to economic events

- Hermes-side production `--dry-run` smoke from a deployed runner before any
  non-dry-run ingestion.
- Prefect deployment for the rolling window (today-7 .. today+60).
- Joining `held` / `watched` flags is still a global ROB-128 follow-up.
```

- [ ] **Step 12.2: Commit**

```bash
git add docs/runbooks/market-events-ingestion.md
git commit -m "docs(market-events): document forexfactory economic ingestion (ROB-132)"
```

---

## Task 13: Local verification & lint

- [ ] **Step 13.1: Lint**

```bash
uv run ruff check app/ tests/ scripts/ingest_market_events.py
uv run ruff format --check app/ tests/ scripts/ingest_market_events.py
```

Expected: clean.

- [ ] **Step 13.2: Backend test sweep**

```bash
uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q
```

Expected: ALL PASS.

- [ ] **Step 13.3: Frontend full check**

```bash
cd frontend/invest
npm run typecheck
npm test
npm run build
```

Expected: ALL PASS / clean build.

- [ ] **Step 13.4: Local dry-run smoke (re-confirm; no DB writes)**

```bash
uv run python -m scripts.ingest_market_events \
  --source forexfactory --category economic --market global \
  --from-date 2026-05-13 --to-date 2026-05-13 --dry-run
```

Verify partition / event / value tables are unchanged from before.

- [ ] **Step 13.5: Push and open PR**

```bash
git push -u origin feature/ROB-132-economic-calendar-discover-events
gh pr create --base main --title "feat(market_events,invest): connect Discover Today Events + ForexFactory economic ingestion (ROB-132)" --body "$(cat <<'EOF'
## Summary
- Wire `/invest/app` Discover `TodayEventCard` to `GET /trading/api/market-events/today` with tabs (전체 / 경제지표 / 실적).
- Add ForexFactory `(source=forexfactory, category=economic, market=global)` ingestion to `market_events`, with forecast/previous/actual stored in `market_event_values`.
- Extend `scripts/ingest_market_events.py` with the new combo, JSON-ish summary line, and dry-run preserved.
- Add `currency` column to `market_events` via Alembic.

Linear: ROB-132.

## Safety
- No broker / order / watch / order-intent / paper / live trading side effects.
- No Prefect deployment or scheduler change.
- No production migration / backfill executed from this PR.
- No direct DB inserts/updates/deletes.
- No Toss-internal API or credentialed scrape.
- News-ingestor is untouched.

## Test plan
- [x] `uv run ruff check ...` clean.
- [x] `uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q` green.
- [x] `cd frontend/invest && npm run typecheck && npm test && npm run build` green.
- [x] Local dry-run CLI prints JSON summary, no DB rows added.
- [ ] Provider live smoke not executed locally — Hermes to run against deployed runner.

## Migration
- `alembic/versions/<rev>_add_market_events_currency.py` adds nullable `currency TEXT` column. Forward-only safe; reversible via `alembic downgrade -1`.

## Follow-ups
- Hermes production `--dry-run` smoke before any non-dry-run ingestion.
- Prefect deployment for rolling window (today-7 .. today+60).
- Held/watched flag join (shared ROB-128 follow-up).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR opened against `main`.

---

## Self-Review Checklist (run before opening PR)

- [ ] **Spec coverage:**
  - Backend: economic-calendar source adapter ✓ (Task 5), normalize to `category=economic` ✓ (Task 4), values in `market_event_values` ✓ (Task 4 + 6), idempotency ✓ (Task 5/6 source_event_id), partition-tracking ✓ (Task 6 reuses repo), failure marking ✓ (Task 6).
  - CLI: extended for `forexfactory/economic/global` ✓ (Task 7), dry-run preserved ✓ (Task 7), JSON summary ✓ (Task 7).
  - API: filters `category=economic` ✓ (Task 8). The schema extends with `currency` ✓ (Task 2).
  - Frontend: card wired to API ✓ (Task 11), loading/error/empty in Korean ✓ (Task 11), tabs 전체/경제지표/실적 ✓ (Task 11), economic forecast/previous/actual rendered ✓ (Task 11), earnings still works ✓ (Task 11), no broker/order mutations ✓ (read-only fetch only).
- [ ] **Placeholder scan:** No "TBD" / "implement later" / "similar to Task N" / "add appropriate validation" remain. Each step shows actual code.
- [ ] **Type consistency:**
  - `normalize_forexfactory_event_row` signature consistent across Tasks 4, 6.
  - `ingest_economic_events_for_date(db, target_date, fetch_rows=...)` signature consistent across Tasks 6, 7.
  - `MarketEvent.currency` referenced consistently in Tasks 1, 2, 4, 8, 9.
  - Front-end type names (`MarketEvent`, `MarketEventsDayResponse`, `FetchMarketEventsTodayParams`) match between Tasks 9, 10, 11.
  - Frontend `fetchMarketEventsToday` parameter shape (`onDate`, `category`, `market`, `source`) is consistent in Tasks 9, 10.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-07-rob-132-economic-calendar-discover-events.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

Which approach?
