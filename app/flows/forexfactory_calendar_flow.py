"""ForexFactory economic-calendar ingestion flow (ROB-184).

Activation is gated on 광현님 approval. Until then the flow is
importable + manually invokable but no Prefect deployment is registered.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from prefect import flow, task

from app.core.db import AsyncSessionLocal
from app.services.market_events.forexfactory_helpers import (
    ForexFactoryWeeklyCache,
    rolling_window_for_today,
)
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
)


@task
async def run_one_day(target_date: date, cache: ForexFactoryWeeklyCache) -> dict:
    async def _fetch(d):
        return await cache.get_events_for_date(d)

    async with AsyncSessionLocal() as db:
        result = await ingest_economic_events_for_date(
            db, target_date, fetch_rows=_fetch
        )
        await db.commit()
        return result.model_dump()


@flow(name="forexfactory_calendar_rolling_window")
async def forexfactory_calendar_rolling_window_flow() -> list[dict]:
    now = datetime.now(UTC)
    start, end = rolling_window_for_today(now)
    cache = ForexFactoryWeeklyCache(now_utc=now)
    out = []
    cur = start
    while cur <= end:
        out.append(await run_one_day(cur, cache))
        cur += timedelta(days=1)
    return out
