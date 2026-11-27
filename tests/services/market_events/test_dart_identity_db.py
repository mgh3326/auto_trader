"""DB-backed: one DART receipt number is one market_events row (hk 6408).

Runs in CI against the run-owned test database. Not run locally (host rule).
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import market_events_test_lock

validate_run_owned_database_url(engine.url)

RECEIPT = "20261127000777"


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session, _market_events_lock):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition

    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


def _dart_event(report_nm: str, category: str) -> dict:
    from app.services.market_events.normalizers import normalize_dart_disclosure_row

    event, _ = normalize_dart_disclosure_row(
        {
            "corp_code": "00000001",
            "corp_name": "Synthetic Listed Co",
            "stock_code": "123456",
            "report_nm": report_nm,
            "rcept_no": RECEIPT,
            "rcept_dt": "20261127",
        }
    )
    assert event["category"] == category
    return event


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reclassified_receipt_moves_the_one_row_instead_of_inserting_another(
    db_session,
):
    from app.models.market_events import MarketEvent
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(_dart_event("감사인지정", "disclosure"), [])
    await repo.upsert_event_with_values(
        _dart_event("분기보고서 (2026.03)", "earnings"), []
    )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "dart",
                    MarketEvent.source_event_id == RECEIPT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].category == "earnings"
    assert rows[0].event_date == date(2026, 11, 27)
    assert rows[0].published_precision == "date"
    assert rows[0].published_date == date(2026, 11, 27)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_amendment_rows_share_a_group_key_and_stay_separate_receipts(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        _dart_event("사업보고서 (2025.12)", "disclosure"), []
    )
    correction = _dart_event("[기재정정]사업보고서 (2025.12)", "disclosure")
    correction["source_event_id"] = "20261127000778"
    await repo.upsert_event_with_values(correction, [])
    await db_session.commit()

    keys = (
        (
            await db_session.execute(
                select(MarketEvent.group_key)
                .where(MarketEvent.source == "dart")
                .order_by(MarketEvent.source_event_id)
            )
        )
        .scalars()
        .all()
    )
    assert keys == ["dart:00000001:사업보고서(2025.12)"] * 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_existing_duplicate_receipt_fails_closed_without_writing(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events.repository import (
        DartIdentityConflict,
        MarketEventsRepository,
    )

    # Two rows for one receipt under different categories: the state the new
    # identity must refuse to resolve by itself (hk 6408 conflict report).
    for category in ("disclosure", "earnings"):
        event = _dart_event("감사인지정", "disclosure")
        event["category"] = category
        db_session.add(MarketEvent(**event))
    await db_session.commit()

    repo = MarketEventsRepository(db_session)
    with pytest.raises(DartIdentityConflict) as raised:
        await repo.upsert_event_with_values(_dart_event("감사인지정", "disclosure"), [])
    assert RECEIPT in str(raised.value)

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MarketEvent)
            .where(MarketEvent.source_event_id == RECEIPT)
        )
    ).scalar_one()
    assert count == 2
