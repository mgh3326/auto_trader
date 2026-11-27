from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.models.market_events import MarketEvent
from app.services.market_events.repository import MarketEventsRepository

DAY = date(2026, 11, 26)
OBSERVED = datetime(2026, 11, 26, 12, tzinfo=UTC)
EVENT = {
    "source": "dart",
    "category": "disclosure",
    "market": "kr",
    "title": "공시",
    "event_date": DAY,
}


def _insert_statement(db):
    """The upsert statement. The DART identity lock may run first (hk 6408)."""
    from sqlalchemy.dialects.postgresql.dml import Insert

    return next(
        call.args[0]
        for call in db.execute.await_args_list
        if isinstance(call.args[0], Insert)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_date_publication_cannot_carry_a_timestamp():
    db = AsyncMock()
    repo = MarketEventsRepository(db)
    with pytest.raises(ValueError, match="date precision requires published_date"):
        await repo.upsert_event_with_values(
            {
                **EVENT,
                "published_precision": "date",
                "published_date": DAY,
                "published_at": OBSERVED,
            },
            [],
        )
    db.execute.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_date_publication_is_written_as_a_date():
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    repo = MarketEventsRepository(db)
    await repo.insert_provider_event(
        {
            **EVENT,
            "source_event_id": "receipt",
            "published_precision": "date",
            "published_date": DAY,
        }
    )
    stmt = db.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    assert stmt.params["published_date"] == DAY
    assert stmt.params["published_at"] is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("later_precision", ["date", "datetime"])
async def test_less_precise_natural_key_observation_preserves_publication_and_first_received(
    later_precision,
):
    db = AsyncMock()
    event = MarketEvent(
        id=1,
        published_at=OBSERVED,
        published_offset="+09:00",
        published_precision="second",
        first_received_at=OBSERVED,
        backfilled=False,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = event
    result.scalar_one.return_value = event
    db.execute.return_value = result
    repo = MarketEventsRepository(db)
    returned = await repo.upsert_event_with_values(
        {
            **EVENT,
            "published_date": DAY if later_precision == "date" else None,
            "published_at": OBSERVED if later_precision == "datetime" else None,
            "published_precision": later_precision,
            "first_received_at": OBSERVED + timedelta(hours=1),
            "backfilled": True,
            "group_key": "operator:42",
        },
        [],
    )
    assert returned.published_at == OBSERVED
    assert returned.published_offset == "+09:00"
    assert returned.published_precision == "second"
    assert returned.published_date is None
    assert returned.first_received_at == OBSERVED
    assert returned.backfilled is False
    assert returned.group_key == "operator:42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_upsert_compares_precision_atomically_and_keeps_first_received():
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    repo = MarketEventsRepository(db)
    await repo.upsert_event_with_values(
        {
            **EVENT,
            "source_event_id": "receipt",
            "published_at": OBSERVED.astimezone(timezone(timedelta(hours=9))),
            "published_offset": "+09:00",
            "published_precision": "datetime",
            "first_received_at": OBSERVED,
            "backfilled": True,
        },
        [],
    )
    compiled = _insert_statement(db).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ON CONFLICT (source, category, market, source_event_id)" in sql
    updates = sql.split("DO UPDATE SET ", 1)[1]
    assert "first_received_at =" not in updates
    assert "backfilled =" not in updates
    assert compiled.params["backfilled"] is True
    assert compiled.params["published_precision"] == "datetime"
    for field in (
        "published_at",
        "published_date",
        "published_offset",
        "published_precision",
    ):
        assert f"{field} = CASE WHEN (CASE market_events.published_precision" in updates
        assert f"THEN excluded.{field} ELSE market_events.{field} END" in updates
    assert compiled.params["published_at"].tzinfo is UTC


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_reingest_keeps_first_observation_and_precise_publication(
    db_session,
):
    repo = MarketEventsRepository(db_session)
    data = {**EVENT, "source_event_id": "hk6406-publication-regression"}
    event = await repo.upsert_event_with_values(
        {
            **data,
            "published_at": OBSERVED,
            "published_precision": "second",
            "published_offset": "+09:00",
            "first_received_at": OBSERVED,
        },
        [],
    )
    assert event.published_at == OBSERVED
    assert await repo.append_source(
        {
            "event_id": event.id,
            "source": data["source"],
            "source_event_id": data["source_event_id"],
            "first_received_at": OBSERVED,
        }
    )
    assert not await repo.append_source(
        {
            "event_id": event.id,
            "source": data["source"],
            "source_event_id": data["source_event_id"],
            "first_received_at": OBSERVED + timedelta(days=1),
        }
    )
    reingested = await repo.upsert_event_with_values(
        {
            **data,
            "published_date": DAY,
            "published_precision": "date",
            "first_received_at": OBSERVED + timedelta(days=1),
        },
        [],
    )
    assert reingested.id == event.id
    assert reingested.first_received_at == OBSERVED
    assert reingested.published_at == OBSERVED
    assert reingested.published_precision == "second"
    assert reingested.published_offset == "+09:00"
    assert reingested.published_date is None
