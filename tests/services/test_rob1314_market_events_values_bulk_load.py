"""ROB-1314 — calendar event values must be bulk-loaded, not queried per event.

`MarketEventsQueryService._query` used to run one `SELECT ... FROM
market_event_values WHERE event_id = :id` per event row (SQL N+1). The fix
loads all values for the range in a single bulk query.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import (
    clean_non_tradingview_market_events,
    market_events_test_lock,
)

validate_run_owned_database_url(engine.url)

_SEED_COUNT = 6
_QUERY_COUNT_CAP = 4


class _CountingSessionProxy:
    """Counts execute() calls and delegates to the real AsyncSession."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.execute_calls = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.execute_calls += 1
        return await self._session.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session, _market_events_lock):
    await clean_non_tradingview_market_events(db_session)
    yield


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_for_range_values_are_bulk_loaded(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    target = date(2099, 5, 20)
    for i in range(_SEED_COUNT):
        await repo.upsert_event_with_values(
            {
                "category": "earnings",
                "market": "us",
                "symbol": f"RB{i:04d}",
                "title": f"ROB1314 N+1 seed {i}",
                "event_date": target,
                "status": "released",
                "source": "finnhub",
                "source_event_id": f"rob1314::nplus1::{i}",
            },
            [
                {
                    "metric_name": "eps",
                    "period": f"Q1-2099-{i}",
                    "actual": Decimal("0.12"),
                    "forecast": Decimal("0.10"),
                    "unit": "USD",
                },
                {
                    "metric_name": "revenue",
                    "period": f"Q1-2099-{i}",
                    "actual": Decimal("1000"),
                    "unit": "USD",
                },
            ],
        )
    await db_session.flush()

    proxy = _CountingSessionProxy(db_session)
    svc = MarketEventsQueryService(proxy)

    response = await svc.list_for_range(target, target)

    assert response.count == _SEED_COUNT
    assert all(len(event.values) == 2 for event in response.events)
    assert proxy.execute_calls <= _QUERY_COUNT_CAP, (
        f"calendar values query fan-out: {proxy.execute_calls} executes "
        f"for {_SEED_COUNT} events (cap {_QUERY_COUNT_CAP})"
    )
