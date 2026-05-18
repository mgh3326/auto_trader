# ROB-264 Finnhub US Earnings Range Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `Market Events Daily/daily` from exhausting the Finnhub quota by replacing 68 per-day calls with one range call grouped by date, plus a skip-already-succeeded path and typed 429 handling.

**Architecture:** Add a new range-aware orchestrator (`ingest_us_earnings_for_range`) alongside the existing per-day function. The CLI dispatches `(finnhub, earnings, us)` to the range path while keeping the per-day function available for narrow recovery and tests. A typed `FinnhubQuotaExceededError` propagates 429 fail-closed; the CLI catches it and reports a clean summary with no partial commits beyond the dates already finalized. A new repository helper exposes "succeeded dates in range" for the pre-filter. No Prefect-flow signature changes — `--from-date`/`--to-date` stays.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest + pytest-asyncio, Pydantic, Finnhub Python SDK, PostgreSQL.

---

## File Structure

**Modify:**
- `app/services/market_events/finnhub_helpers.py` — wrap SDK 429s in a typed `FinnhubQuotaExceededError`.
- `app/services/market_events/ingestion.py` — add `ingest_us_earnings_for_range` (per-day function stays).
- `app/services/market_events/repository.py` — add `list_succeeded_partitions_in_range`.
- `scripts/ingest_market_events.py` — branch `(finnhub, earnings, us)` to the range path; add `--force` (orchestrator-level skip override).
- `docs/runbooks/market-events-ingestion.md` — add safe-recovery section + `--force` usage note.

**Modify (tests):**
- `tests/services/test_market_events_ingestion.py` — range orchestrator behaviors (group-by-date, zero-event days, skip-succeeded, 429 fail-closed, force replay).
- `tests/services/test_market_events_repository.py` — `list_succeeded_partitions_in_range` returns only succeeded dates.
- `tests/test_market_events_cli.py` — CLI uses range path for `finnhub/earnings/us`; `--force` propagation; dry-run does not call orchestrator.

**Create:**
- `tests/services/test_market_events_finnhub_helpers.py` — typed-exception coverage for 429 mapping (other status codes pass through).

---

## Task 1: Typed Finnhub 429 Exception

**Files:**
- Modify: `app/services/market_events/finnhub_helpers.py`
- Create: `tests/services/test_market_events_finnhub_helpers.py`

- [ ] **Step 1: Write failing test for 429 → typed exception**

Create `tests/services/test_market_events_finnhub_helpers.py`:

```python
"""Tests for finnhub_helpers 429 mapping (ROB-264)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeFinnhubAPIException(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_maps_429_to_quota_error(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=429,
        message="API limit reached. Please try again later. Remaining Limit: 0",
    )
    monkeypatch.setattr(
        finnhub_helpers, "_get_finnhub_client", lambda: fake_client
    )

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-07-17"
        )

    assert exc_info.value.status_code == 429
    assert "API limit" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_passes_through_non_429(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=500, message="upstream boom"
    )
    monkeypatch.setattr(
        finnhub_helpers, "_get_finnhub_client", lambda: fake_client
    )

    with pytest.raises(_FakeFinnhubAPIException):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-05-11"
        )
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `uv run pytest tests/services/test_market_events_finnhub_helpers.py -v`
Expected: FAIL — `FinnhubQuotaExceededError` does not exist on the module.

- [ ] **Step 3: Add typed exception + 429 catch in finnhub_helpers**

Edit `app/services/market_events/finnhub_helpers.py`. Add the exception class near the top (after imports) and wrap the `fetch_sync` call.

Replace the current body of `fetch_earnings_calendar_finnhub` (the part inside `try` is new):

```python
"""Finnhub market-events fetch helpers.

Kept under the service layer so ingestion does not import MCP tooling modules at
module import time. The Finnhub SDK/settings imports stay lazy so unit tests can
monkeypatch fetchers without requiring production credentials.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any


class FinnhubQuotaExceededError(Exception):
    """Raised when Finnhub returns HTTP 429 (daily/per-minute quota exhausted).

    Callers should treat this as fail-closed: do not retry the same call within
    the same run, and do not continue iterating remaining partitions.
    """

    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = status_code


def _get_finnhub_client() -> Any:
    try:
        import finnhub
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "finnhub-python is required to use Finnhub providers"
        ) from exc

    from app.core.config import settings

    api_key = settings.finnhub_api_key
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub earningsCalendar rows for ingestion.

    Raises FinnhubQuotaExceededError on HTTP 429; all other SDK exceptions
    propagate unchanged.
    """
    client = _get_finnhub_client()

    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    def fetch_sync() -> dict[str, Any]:
        return client.earnings_calendar(
            symbol=symbol.upper() if symbol else "",
            _from=from_date,
            to=to_date,
        )

    try:
        result = await asyncio.to_thread(fetch_sync)
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            raise FinnhubQuotaExceededError(str(exc), status_code=429) from exc
        raise

    if not result or not result.get("earningsCalendar"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": from_date,
            "to_date": to_date,
            "count": 0,
            "earnings": [],
        }

    earnings = []
    for item in result.get("earningsCalendar", []):
        earnings.append(
            {
                "symbol": item.get("symbol", ""),
                "date": item.get("date"),
                "hour": item.get("hour", ""),
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "from_date": from_date,
        "to_date": to_date,
        "count": len(earnings),
        "earnings": earnings,
    }
```

- [ ] **Step 4: Run test, confirm it passes**

Run: `uv run pytest tests/services/test_market_events_finnhub_helpers.py -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/finnhub_helpers.py tests/services/test_market_events_finnhub_helpers.py
git commit -m "$(cat <<'EOF'
feat(market-events): add FinnhubQuotaExceededError for 429 fail-closed (ROB-264)

Wrap Finnhub SDK 429 responses in a typed domain exception so the upcoming
range-aware ingestion path can abort immediately without burning through the
remaining partitions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Repository — list_succeeded_partitions_in_range

**Files:**
- Modify: `app/services/market_events/repository.py`
- Modify: `tests/services/test_market_events_repository.py`

- [ ] **Step 1: Write failing test**

Append to `tests/services/test_market_events_repository.py` (at the end of the file):

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_succeeded_partitions_in_range(db_session):
    from datetime import date

    from sqlalchemy import delete

    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events.repository import MarketEventsRepository

    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()

    repo = MarketEventsRepository(db_session)

    succeeded_dates = [date(2026, 5, 11), date(2026, 5, 12)]
    failed_dates = [date(2026, 5, 13)]
    other_market_date = date(2026, 5, 11)

    for d in succeeded_dates:
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_succeeded(p, event_count=3)
    for d in failed_dates:
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_failed(p, error="boom")
    other = await repo.get_or_create_partition(
        source="dart",
        category="disclosure",
        market="kr",
        partition_date=other_market_date,
    )
    await repo.mark_partition_succeeded(other, event_count=1)
    await db_session.commit()

    result = await repo.list_succeeded_partitions_in_range(
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
    )

    assert result == {date(2026, 5, 11), date(2026, 5, 12)}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_succeeded_partitions_in_range_empty(db_session):
    from datetime import date

    from sqlalchemy import delete

    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events.repository import MarketEventsRepository

    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()

    repo = MarketEventsRepository(db_session)

    result = await repo.list_succeeded_partitions_in_range(
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
    )

    assert result == set()
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `uv run pytest tests/services/test_market_events_repository.py::test_list_succeeded_partitions_in_range tests/services/test_market_events_repository.py::test_list_succeeded_partitions_in_range_empty -v`
Expected: FAIL — method does not exist.

- [ ] **Step 3: Add helper to repository**

Edit `app/services/market_events/repository.py`. Append the method to the `MarketEventsRepository` class (after `mark_partition_failed`):

```python
    async def list_succeeded_partitions_in_range(
        self,
        *,
        source: str,
        category: str,
        market: str,
        from_date: date,
        to_date: date,
    ) -> set[date]:
        """Return the set of `partition_date`s with status='succeeded' in the
        inclusive window.

        Used to skip already-succeeded partitions during rolling-window ingestion
        and avoid unnecessarily reconsuming external API quota on a rerun.

        Note: callers MUST NOT use `event_count > 0` as a substitute for
        `status='succeeded'`. A failed partition can still carry a non-zero
        `event_count` from an earlier partial run.
        """
        stmt = select(MarketEventIngestionPartition.partition_date).where(
            MarketEventIngestionPartition.source == source,
            MarketEventIngestionPartition.category == category,
            MarketEventIngestionPartition.market == market,
            MarketEventIngestionPartition.status == "succeeded",
            MarketEventIngestionPartition.partition_date >= from_date,
            MarketEventIngestionPartition.partition_date <= to_date,
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return set(rows)
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `uv run pytest tests/services/test_market_events_repository.py::test_list_succeeded_partitions_in_range tests/services/test_market_events_repository.py::test_list_succeeded_partitions_in_range_empty -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/repository.py tests/services/test_market_events_repository.py
git commit -m "$(cat <<'EOF'
feat(market-events): add list_succeeded_partitions_in_range helper (ROB-264)

Exposes succeeded partition dates per (source, category, market) so the
upcoming range-aware ingestion path can skip already-ingested days and avoid
reconsuming external API quota on reruns. `status='succeeded'` is the
authoritative signal — `event_count` alone is not.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3a: Range Orchestrator — happy path

**Files:**
- Modify: `app/services/market_events/ingestion.py`
- Modify: `tests/services/test_market_events_ingestion.py`

- [ ] **Step 1: Write failing test for group-by-date + zero-event days**

Append to `tests/services/test_market_events_ingestion.py`:

```python
FINNHUB_RESPONSE_RANGE_MULTI_DATE = {
    "symbol": None,
    "instrument_type": "equity_us",
    "source": "finnhub",
    "from_date": "2026-05-11",
    "to_date": "2026-05-13",
    "count": 3,
    "earnings": [
        {
            "symbol": "AAA",
            "date": "2026-05-11",
            "hour": "bmo",
            "eps_estimate": 1.0,
            "eps_actual": None,
            "revenue_estimate": 100,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
        {
            "symbol": "BBB",
            "date": "2026-05-11",
            "hour": "amc",
            "eps_estimate": 2.0,
            "eps_actual": None,
            "revenue_estimate": 200,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
        {
            "symbol": "CCC",
            "date": "2026-05-13",
            "hour": "amc",
            "eps_estimate": 3.0,
            "eps_actual": None,
            "revenue_estimate": 300,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
    ],
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_groups_by_date(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )
    await db_session.commit()

    fake.assert_awaited_once_with(None, "2026-05-11", "2026-05-13")

    assert [r.partition_date for r in results] == [
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    ]
    assert [r.status for r in results] == ["succeeded", "succeeded", "succeeded"]
    assert [r.event_count for r in results] == [2, 0, 1]

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert sorted(e.symbol for e in events) == ["AAA", "BBB", "CCC"]

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    parts_by_date = {p.partition_date: p for p in parts}
    assert set(parts_by_date.keys()) == {
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    }
    assert parts_by_date[date(2026, 5, 12)].status == "succeeded"
    assert parts_by_date[date(2026, 5, 12)].event_count == 0
```

- [ ] **Step 2: Run test, confirm it fails**

Run: `uv run pytest tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_groups_by_date -v`
Expected: FAIL — `ingest_us_earnings_for_range` does not exist.

- [ ] **Step 3: Implement range orchestrator (happy path only)**

Edit `app/services/market_events/ingestion.py`. Add the following imports at the top if missing:

```python
from datetime import date, timedelta
```

(Replace the existing `from datetime import date` with the line above.)

Then append after `ingest_us_earnings_for_date`:

```python
async def ingest_us_earnings_for_range(
    db: AsyncSession,
    from_date: date,
    to_date: date,
    *,
    skip_succeeded: bool = True,
) -> list[IngestionRunResult]:
    """Range-aware US earnings ingestion (ROB-264).

    Calls Finnhub once for the entire [from_date, to_date] window, groups rows
    by `event_date`, and writes one `market_event_ingestion_partitions` row per
    day in the window — including days with zero events.

    When `skip_succeeded=True` (default), days already marked
    `status='succeeded'` in the partition table are left untouched. Use
    `skip_succeeded=False` for explicit replay.

    Raises:
        FinnhubQuotaExceededError: 429 from Finnhub. No partitions are mutated;
            callers should fail closed and retry after quota resets.
    """
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    source = "finnhub"
    category = "earnings"
    market = "us"
    repo = MarketEventsRepository(db)

    all_dates: list[date] = []
    cur = from_date
    while cur <= to_date:
        all_dates.append(cur)
        cur += timedelta(days=1)

    succeeded_set: set[date] = set()
    if skip_succeeded:
        succeeded_set = await repo.list_succeeded_partitions_in_range(
            source=source,
            category=category,
            market=market,
            from_date=from_date,
            to_date=to_date,
        )

    dates_to_process = [d for d in all_dates if d not in succeeded_set]
    if not dates_to_process:
        logger.info(
            "all %d partitions already succeeded for %s..%s; skipping fetch",
            len(all_dates),
            from_date,
            to_date,
        )
        return []

    response = await fetch_earnings_calendar_finnhub(
        None, from_date.isoformat(), to_date.isoformat()
    )
    rows = response.get("earnings", []) if isinstance(response, dict) else []

    rows_by_date: dict[date, list[dict[str, Any]]] = {d: [] for d in dates_to_process}
    for row in rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            ev_date = date.fromisoformat(raw_date)
        except ValueError:
            logger.warning("skipping finnhub row with bad date: %s", row)
            continue
        if ev_date in rows_by_date:
            rows_by_date[ev_date].append(row)

    results: list[IngestionRunResult] = []
    for d in dates_to_process:
        partition = await repo.get_or_create_partition(
            source=source,
            category=category,
            market=market,
            partition_date=d,
        )
        await repo.mark_partition_running(partition)
        try:
            upserted = 0
            for row in rows_by_date[d]:
                try:
                    event_dict, value_dicts = normalize_finnhub_earnings_row(row)
                except ValueError as exc:
                    logger.warning(
                        "skipping unparseable finnhub row: %s (%s)", row, exc
                    )
                    continue
                await repo.upsert_event_with_values(event_dict, value_dicts)
                upserted += 1
            await repo.mark_partition_succeeded(partition, event_count=upserted)
            await db.commit()
            results.append(
                IngestionRunResult(
                    source=source,
                    category=category,
                    market=market,
                    partition_date=d,
                    status="succeeded",
                    event_count=upserted,
                )
            )
        except Exception as exc:
            logger.exception("finnhub earnings ingestion failed for %s", d)
            failed = await _mark_failed_after_exception(
                db,
                source=source,
                category=category,
                market=market,
                partition_date=d,
                error=exc,
            )
            await db.commit()
            results.append(failed)

    return results
```

Also import the helper `fetch_earnings_calendar_finnhub` is already imported. Confirm `FinnhubQuotaExceededError` is reachable for later tasks — no new import needed yet because Task 3c references it via `app.services.market_events.finnhub_helpers`.

- [ ] **Step 4: Run test, confirm it passes**

Run: `uv run pytest tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_groups_by_date -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/market_events/ingestion.py tests/services/test_market_events_ingestion.py
git commit -m "$(cat <<'EOF'
feat(market-events): add ingest_us_earnings_for_range happy path (ROB-264)

Single Finnhub API call for the full window, grouped per event_date, one
partition row per day including zero-event days. Per-day function retained
for narrow recovery and single-day tests.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3b: Range Orchestrator — skip already-succeeded

**Files:**
- Modify: `tests/services/test_market_events_ingestion.py`

- [ ] **Step 1: Write failing test (skip-by-default + force replays)**

Append to `tests/services/test_market_events_ingestion.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_skips_succeeded_by_default(
    db_session, monkeypatch
):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    for d in (date(2026, 5, 11), date(2026, 5, 12)):
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_succeeded(p, event_count=1)
    await db_session.commit()

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )
    await db_session.commit()

    fake.assert_awaited_once()
    assert [r.partition_date for r in results] == [date(2026, 5, 13)]
    assert results[0].status == "succeeded"
    assert results[0].event_count == 1

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    parts_by_date = {p.partition_date: p for p in parts}
    assert parts_by_date[date(2026, 5, 11)].event_count == 1
    assert parts_by_date[date(2026, 5, 12)].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_force_replays_succeeded(
    db_session, monkeypatch
):
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 11),
    )
    await repo.mark_partition_succeeded(p, event_count=99)
    await db_session.commit()

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session,
        date(2026, 5, 11),
        date(2026, 5, 13),
        skip_succeeded=False,
    )
    await db_session.commit()

    assert [r.partition_date for r in results] == [
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    ]
    assert results[0].event_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_all_succeeded_skips_fetch(
    db_session, monkeypatch
):
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    for d in (date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)):
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_succeeded(p, event_count=0)
    await db_session.commit()

    fake = AsyncMock()
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )

    fake.assert_not_awaited()
    assert results == []
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_skips_succeeded_by_default tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_force_replays_succeeded tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_all_succeeded_skips_fetch -v`
Expected: PASS (the orchestrator already implements skip-by-default and skip-all from Task 3a).

If any fail, fix the orchestrator before continuing — likely an off-by-one in `dates_to_process` or `skip_succeeded` branch.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_market_events_ingestion.py
git commit -m "$(cat <<'EOF'
test(market-events): cover range skip-succeeded and force replay (ROB-264)

Verifies that already-succeeded partitions are not reconsumed by default and
that skip_succeeded=False explicitly replays. Also asserts the orchestrator
skips the Finnhub fetch entirely when no partitions need work.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3c: Range Orchestrator — 429 fail-closed

**Files:**
- Modify: `tests/services/test_market_events_ingestion.py`

- [ ] **Step 1: Write failing test for 429 fail-closed**

Append to `tests/services/test_market_events_ingestion.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_429_is_fail_closed(
    db_session, monkeypatch
):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion
    from app.services.market_events.finnhub_helpers import (
        FinnhubQuotaExceededError,
    )

    fake = AsyncMock(side_effect=FinnhubQuotaExceededError("limit reached"))
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    with pytest.raises(FinnhubQuotaExceededError):
        await ingestion.ingest_us_earnings_for_range(
            db_session, date(2026, 5, 11), date(2026, 5, 13)
        )
    await db_session.rollback()

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert parts == []
```

- [ ] **Step 2: Run test, confirm it passes**

Run: `uv run pytest tests/services/test_market_events_ingestion.py::test_ingest_us_earnings_for_range_429_is_fail_closed -v`
Expected: PASS — the orchestrator from Task 3a re-raises any exception from `fetch_earnings_calendar_finnhub`, and no partitions were created yet (the per-day claim happens after fetch).

If it fails (e.g., a partition row was created), tighten the orchestrator to ensure no partition writes occur before the fetch succeeds.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_market_events_ingestion.py
git commit -m "$(cat <<'EOF'
test(market-events): verify range orchestrator fails closed on 429 (ROB-264)

When Finnhub raises FinnhubQuotaExceededError before any partition is claimed,
no partition rows are mutated and the exception propagates to the caller.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI — dispatch range path + --force

**Files:**
- Modify: `scripts/ingest_market_events.py`
- Modify: `tests/test_market_events_cli.py`

- [ ] **Step 1: Write failing tests for CLI dispatch**

Append to `tests/test_market_events_cli.py`:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_finnhub_earnings_us_uses_range_path(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake_range = AsyncMock(return_value=[])
    monkeypatch.setattr(cli, "ingest_us_earnings_for_range", fake_range)

    per_day = AsyncMock()
    monkeypatch.setitem(cli.SUPPORTED, ("finnhub", "earnings", "us"), per_day)

    rc = await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
        dry_run=False,
        force=False,
    )

    assert rc == 0
    fake_range.assert_awaited_once_with(
        db_session,
        date(2026, 5, 11),
        date(2026, 5, 13),
        skip_succeeded=True,
    )
    per_day.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_finnhub_earnings_us_force_disables_skip(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake_range = AsyncMock(return_value=[])
    monkeypatch.setattr(cli, "ingest_us_earnings_for_range", fake_range)

    rc = await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
        dry_run=False,
        force=True,
    )

    assert rc == 0
    fake_range.assert_awaited_once_with(
        db_session,
        date(2026, 5, 11),
        date(2026, 5, 13),
        skip_succeeded=False,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_finnhub_earnings_us_dry_run_does_not_call_range(
    db_session, monkeypatch
):
    from scripts import ingest_market_events as cli

    fake_range = AsyncMock()
    monkeypatch.setattr(cli, "ingest_us_earnings_for_range", fake_range)

    rc = await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
        dry_run=True,
        force=False,
    )

    assert rc == 0
    fake_range.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_finnhub_earnings_us_429_marks_failed_summary(
    db_session, monkeypatch, capsys
):
    from app.services.market_events.finnhub_helpers import (
        FinnhubQuotaExceededError,
    )
    from scripts import ingest_market_events as cli

    fake_range = AsyncMock(side_effect=FinnhubQuotaExceededError("limit reached"))
    monkeypatch.setattr(cli, "ingest_us_earnings_for_range", fake_range)

    rc = await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 13),
        dry_run=False,
        force=False,
    )

    assert rc == 2
    import json as _json

    summary = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["failed"] >= 1
    assert summary["succeeded"] == 0
    assert summary.get("error") == "finnhub_quota_exceeded"


@pytest.mark.unit
def test_parse_args_force_flag():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        [
            "--source",
            "finnhub",
            "--category",
            "earnings",
            "--market",
            "us",
            "--from-date",
            "2026-05-11",
            "--to-date",
            "2026-05-13",
            "--force",
        ]
    )
    assert ns.force is True


@pytest.mark.unit
def test_parse_args_force_defaults_false():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        [
            "--from-date",
            "2026-05-11",
            "--to-date",
            "2026-05-13",
        ]
    )
    assert ns.force is False
```

Also update `tests/test_market_events_cli.py:test_run_ingest_dispatches_per_day` — it currently mounts `(finnhub, earnings, us)` in `SUPPORTED` but the CLI will no longer route there. Pin that existing test to a different source (e.g., `wisefn`) so the per-day dispatch coverage remains:

Find the function `test_run_ingest_dispatches_per_day` and replace it with:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_dispatches_per_day(db_session, monkeypatch):
    """Per-day dispatch path still works for non-finnhub sources."""
    from app.core import config as config_mod
    from scripts import ingest_market_events as cli

    monkeypatch.setattr(config_mod.settings, "wisefn_earnings_enabled", True)

    fake = AsyncMock(
        return_value=type("R", (), {"status": "succeeded", "event_count": 0})()
    )
    monkeypatch.setitem(cli.SUPPORTED, ("wisefn", "earnings", "kr"), fake)

    await cli.run_ingest(
        db=db_session,
        source="wisefn",
        category="earnings",
        market="kr",
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 9),
        dry_run=False,
        force=False,
    )
    assert fake.await_count == 3
```

Existing tests that call `cli.run_ingest(...)` without `force=` must be updated to pass `force=False` — apply the same one-line addition to:
- `test_run_ingest_dry_run_does_not_call_orchestrator`
- `test_run_ingest_skips_wisefn_when_flag_disabled`
- `test_run_ingest_calls_wisefn_when_flag_enabled`
- `test_cli_forexfactory_run_reuses_single_cache_across_days`
- `test_run_ingest_dispatches_tradingview_economic_global`

Add `force=False,` as a kwarg to each `cli.run_ingest(...)` call.

- [ ] **Step 2: Run new tests, confirm they fail**

Run: `uv run pytest tests/test_market_events_cli.py::test_cli_finnhub_earnings_us_uses_range_path tests/test_market_events_cli.py::test_parse_args_force_flag -v`
Expected: FAIL — `run_ingest` does not accept `force`; `ingest_us_earnings_for_range` not imported in CLI; `--force` not in argparse.

- [ ] **Step 3: Update CLI**

Edit `scripts/ingest_market_events.py`:

(a) Add to imports near the top (after the existing `from app.services.market_events.ingestion import (...)` block):

```python
from app.services.market_events.finnhub_helpers import (
    FinnhubQuotaExceededError,
)
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_kr_earnings_wisefn_for_date,
    ingest_tradingview_economic_events_for_date,
    ingest_us_earnings_for_date,
    ingest_us_earnings_for_range,
)
```

(b) In `parse_args`, after the `--dry-run` line, add:

```python
    parser.add_argument(
        "--force",
        action="store_true",
        dest="force",
        help=(
            "Reprocess already-succeeded partitions. Default skips them to "
            "preserve external API quota on reruns."
        ),
    )
```

(c) Update `run_ingest` signature and body. Replace the existing function with:

```python
async def run_ingest(
    *,
    db: AsyncSession,
    source: str,
    category: str,
    market: str,
    from_date: date,
    to_date: date,
    dry_run: bool,
    force: bool = False,
) -> int:
    enabled, reason = _is_source_enabled(source, category, market)
    if not enabled and not dry_run:
        logger.warning("%s; skipping run for %s..%s", reason, from_date, to_date)
        return 0

    # Range-aware path: one Finnhub API call per CLI invocation (ROB-264).
    if (source, category, market) == ("finnhub", "earnings", "us"):
        return await _run_finnhub_us_earnings_range(
            db=db,
            from_date=from_date,
            to_date=to_date,
            dry_run=dry_run,
            force=force,
        )

    fn = SUPPORTED[(source, category, market)]

    ff_cache = None
    if (source, category, market) == ("forexfactory", "economic", "global"):
        from app.services.market_events.forexfactory_helpers import (
            ForexFactoryWeeklyCache,
        )

        ff_cache = ForexFactoryWeeklyCache()

    succeeded = 0
    failed = 0
    for d in iter_partition_dates(from_date, to_date):
        if dry_run:
            logger.info(
                "[DRY-RUN] would ingest %s/%s/%s for %s", source, category, market, d
            )
            succeeded += 1
            continue
        if ff_cache is not None:
            _cache = ff_cache

            async def _fetch_with_cache(target_date, _c=_cache):
                return await _c.get_events_for_date(target_date)

            result = await fn(db, d, fetch_rows=_fetch_with_cache)
        else:
            result = await fn(db, d)
        await db.commit()
        if result.status == "succeeded":
            succeeded += 1
            logger.info(
                "ingested %s events for %s/%s/%s on %s",
                result.event_count,
                source,
                category,
                market,
                d,
            )
        else:
            failed += 1
            logger.error(
                "ingest failed for %s/%s/%s on %s: %s",
                source,
                category,
                market,
                d,
                result.error,
            )
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


async def _run_finnhub_us_earnings_range(
    *,
    db: AsyncSession,
    from_date: date,
    to_date: date,
    dry_run: bool,
    force: bool,
) -> int:
    """Single-call range path for (finnhub, earnings, us) (ROB-264)."""
    source = "finnhub"
    category = "earnings"
    market = "us"

    if dry_run:
        partition_count = (to_date - from_date).days + 1
        logger.info(
            "[DRY-RUN] would range-ingest %s/%s/%s for %s..%s (%d partitions, force=%s)",
            source,
            category,
            market,
            from_date,
            to_date,
            partition_count,
            force,
        )
        summary = {
            "source": source,
            "category": category,
            "market": market,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "dry_run": True,
            "succeeded": partition_count,
            "failed": 0,
        }
        import json as _json

        print(_json.dumps(summary))
        return 0

    error_label: str | None = None
    succeeded = 0
    failed = 0
    try:
        results = await ingest_us_earnings_for_range(
            db,
            from_date,
            to_date,
            skip_succeeded=not force,
        )
        for r in results:
            if r.status == "succeeded":
                succeeded += 1
                logger.info(
                    "ingested %s events for %s/%s/%s on %s",
                    r.event_count,
                    source,
                    category,
                    market,
                    r.partition_date,
                )
            else:
                failed += 1
                logger.error(
                    "ingest failed for %s/%s/%s on %s: %s",
                    source,
                    category,
                    market,
                    r.partition_date,
                    r.error,
                )
    except FinnhubQuotaExceededError as exc:
        error_label = "finnhub_quota_exceeded"
        logger.error(
            "finnhub quota exhausted for %s..%s; failing closed without "
            "mutating remaining partitions: %s",
            from_date,
            to_date,
            exc,
        )
        failed += 1

    summary: dict[str, Any] = {
        "source": source,
        "category": category,
        "market": market,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dry_run": False,
        "succeeded": succeeded,
        "failed": failed,
    }
    if error_label:
        summary["error"] = error_label
    import json as _json

    print(_json.dumps(summary))
    logger.info("ingest complete: %s", summary)
    return 0 if failed == 0 else 2
```

Add this import at the top of the file (near the other `from typing`/stdlib imports — if `typing` is not yet imported, add it):

```python
from typing import Any
```

(d) Update `main` to pass `force`:

```python
async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="market-events-ingest")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_ingest(
                db=db,
                source=ns.source,
                category=ns.category,
                market=ns.market,
                from_date=ns.from_date,
                to_date=ns.to_date,
                dry_run=ns.dry_run,
                force=ns.force,
            )
    except Exception as exc:
        capture_exception(exc, process="ingest_market_events")
        logger.error("ingest_market_events crashed: %s", exc, exc_info=True)
        return 1
```

- [ ] **Step 4: Run all CLI tests**

Run: `uv run pytest tests/test_market_events_cli.py -v`
Expected: PASS for all (new + existing-with-force-added).

- [ ] **Step 5: Commit**

```bash
git add scripts/ingest_market_events.py tests/test_market_events_cli.py
git commit -m "$(cat <<'EOF'
feat(market-events-cli): route finnhub/earnings/us through range path (ROB-264)

CLI now invokes ingest_us_earnings_for_range once per run for the US earnings
source, replacing the 68-call per-day loop that exhausted Finnhub quota in
Market Events Daily/daily. New --force flag disables skip-already-succeeded
for explicit replays. 429s fail closed with summary.error="finnhub_quota_exceeded".

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Runbook — safe recovery and --force usage

**Files:**
- Modify: `docs/runbooks/market-events-ingestion.md`

- [ ] **Step 1: Add the safe-recovery section**

Edit `docs/runbooks/market-events-ingestion.md`. After the "## CLI" section (immediately before "## Read API"), insert:

````markdown
## US earnings: range-aware ingestion (ROB-264)

`(source, category, market) == (finnhub, earnings, us)` is routed through
`ingest_us_earnings_for_range` in `app/services/market_events/ingestion.py`,
which issues **one** Finnhub `earningsCalendar` call for the full
`--from-date`..`--to-date` window and writes one partition row per day
(including zero-event days).

Already-succeeded partitions are skipped by default to preserve quota on
reruns. The CLI/orchestrator uses `partition.status='succeeded'` as the
authoritative skip signal — `event_count > 0` is NOT a substitute, because a
failed partition can still carry a non-zero `event_count` from an earlier
partial run.

### Safe recovery after a 429

If a daily run hits a Finnhub quota error, **do not** rerun the full rolling
window. Doing so reconsumes quota on already-succeeded dates and is likely to
hit 429 again before reaching the failed tail.

Recommended recovery paths, in order of preference:

1. **Narrow recovery for the failed tail after quota reset.** Rerun with a tight
   `--from-date`/`--to-date` covering just the failed partitions (look up
   the failed range in `market_event_ingestion_partitions` where
   `status='failed'` and `last_error LIKE '%FinnhubAPI%429%'`).
2. **Rerun the normal rolling window only if the failed range is unknown.**
   The range-aware path still makes one external Finnhub call for the missing
   span, while skip-already-succeeded preserves partition rows and avoids DB
   rewrites for completed dates.
3. **Force replay for a known-stale window.** Pass `--force` to reprocess
   already-succeeded partitions. This consumes quota for the full window and
   is only appropriate when the upstream data has been corrected.

### CLI examples

```bash
# Normal rolling window (already-succeeded dates are skipped automatically)
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-11 --to-date 2026-07-17

# Force replay of an already-ingested range (consumes quota)
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-11 --to-date 2026-05-17 --force

# Narrow recovery of a failed tail
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-07-10 --to-date 2026-07-17
```

On a 429, the CLI exits with code `2` and prints a summary line whose JSON
includes `"error": "finnhub_quota_exceeded"` and `"aborted": true`. No
partition rows are mutated on the failed call; partition state from previous
successful runs is preserved as-is.
````

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/market-events-ingestion.md
git commit -m "$(cat <<'EOF'
docs(market-events): document range-aware US earnings ingest + safe recovery (ROB-264)

Adds operator guidance for the new single-call range path, the
skip-already-succeeded default, --force replay, and the 429 fail-closed
recovery procedure. Calls out the partial-state caution that
status='succeeded' (not event_count) is the authoritative skip signal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Full verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the affected test files**

```bash
uv run pytest \
  tests/services/test_market_events_finnhub_helpers.py \
  tests/services/test_market_events_repository.py \
  tests/services/test_market_events_ingestion.py \
  tests/test_market_events_cli.py \
  -v
```

Expected: ALL PASS.

- [ ] **Step 2: Run lint + format**

```bash
uv run ruff check app/services/market_events scripts/ingest_market_events.py tests/services tests/test_market_events_cli.py
uv run ruff format --check app/services/market_events scripts/ingest_market_events.py tests/services tests/test_market_events_cli.py
```

Expected: no errors. If `ruff format --check` fails, run `uv run ruff format <paths>` and commit the format diff with `style: ruff format (ROB-264)`.

- [ ] **Step 3: Run typecheck**

```bash
uv run ty check app/services/market_events scripts/ingest_market_events.py
```

Expected: no errors. If `ty` is not installed or fails for reasons unrelated to this PR (pre-existing issues), note it in the PR description rather than masking.

- [ ] **Step 4: Run full market-events test slice once more**

```bash
make test-unit
```

Expected: PASS (this PR adds no slow integration paths to `unit`).

- [ ] **Step 5: Open PR**

```bash
git push -u origin rob-264
gh pr create --title "fix(market-events): range-aware US earnings ingest (ROB-264)" --body "$(cat <<'EOF'
## Summary

- Replaces the 68-call per-day Finnhub loop in `Market Events Daily/daily` with a single range call grouped by event date.
- Adds a typed `FinnhubQuotaExceededError` so 429s fail closed without mutating remaining partitions.
- Adds skip-already-succeeded by default + `--force` opt-out for explicit replays.
- Documents safe-recovery procedure in the market-events ingestion runbook.

## Test plan

- [ ] `uv run pytest tests/services/test_market_events_finnhub_helpers.py tests/services/test_market_events_repository.py tests/services/test_market_events_ingestion.py tests/test_market_events_cli.py -v`
- [ ] `uv run ruff check` clean on touched paths
- [ ] `uv run ruff format --check` clean on touched paths
- [ ] Manual sanity: dry-run on a 68-day window logs a single planned range call and no API hit
- [ ] Manual sanity: rerun after partial success skips already-succeeded dates (one fetch, fewer DB writes)

Closes ROB-264.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes

- Spec coverage:
  - Range fetch grouped by date → Task 3a.
  - Zero-event dates marked succeeded → Task 3a test.
  - Skip already-succeeded → Task 3b tests; pre-filter in orchestrator.
  - 429 backoff/fail-closed → Task 1 (typed exc) + Task 3c (orchestrator) + Task 4 (CLI summary).
  - No quota reconsumption on rerun → Task 3b first test.
  - Operational docs → Task 5.
- Acceptance constraint "no broker/order/watch/order-intent side effects" — verified: all touched code is in the market-events ingestion boundary; no broker or watch helpers are imported.
- Acceptance constraint "do not print or store API credentials" — verified: `_redact_sensitive_keys` path in `repository.upsert_event_with_values` is unchanged; the new orchestrator never inspects credentials.
