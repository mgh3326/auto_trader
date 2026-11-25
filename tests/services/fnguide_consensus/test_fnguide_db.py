"""hk 6508: snapshot writer and read model against the CI database."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.models.fnguide import FnguideReport, FnguideSnapshot, FnguideValue
from app.services.fnguide_consensus.query import read_latest_consensus
from app.services.fnguide_consensus.writer import FnguideSnapshotWriter
from tests.services.fnguide_consensus._helpers import OBSERVED, parse_fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SYMBOLS = ("005930", "028300")


async def _clean(session) -> None:
    await session.execute(
        delete(FnguideReport).where(FnguideReport.symbol.in_(SYMBOLS))
    )
    await session.execute(
        delete(FnguideValue).where(
            FnguideValue.snapshot_id.in_(
                select(FnguideSnapshot.id).where(FnguideSnapshot.symbol.in_(SYMBOLS))
            )
        )
    )
    await session.execute(
        delete(FnguideSnapshot).where(FnguideSnapshot.symbol.in_(SYMBOLS))
    )
    await session.commit()


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_same_day_rerun_keeps_every_row_count(db_session) -> None:
    await _clean(db_session)
    writer = FnguideSnapshotWriter(db_session)
    parsed = parse_fixture("005930_consensus.html", symbol="005930")

    first = await writer.write_page(parsed)
    await db_session.commit()
    before = (
        await _count(db_session, FnguideSnapshot),
        await _count(db_session, FnguideValue),
    )

    second = await writer.write_page(parsed)
    await db_session.commit()
    after = (
        await _count(db_session, FnguideSnapshot),
        await _count(db_session, FnguideValue),
    )

    assert first.inserted is True
    assert second.inserted is False
    assert before == after


async def test_no_values_snapshot_round_trips_with_its_reason(db_session) -> None:
    await _clean(db_session)
    parsed = parse_fixture("028300_consensus.html", symbol="028300")
    await FnguideSnapshotWriter(db_session).write_page(parsed)
    await db_session.commit()

    view = await read_latest_consensus(db_session, "028300")
    assert view is not None
    assert view["reason"] == "all_consensus_values_null"
    assert view["items"] == []
    assert view["observed_date"] == OBSERVED.isoformat()


async def test_read_returns_target_price_revision(db_session) -> None:
    await _clean(db_session)
    parsed = parse_fixture("005930_consensus.html", symbol="005930")
    await FnguideSnapshotWriter(db_session).write_page(parsed)
    await db_session.commit()

    view = await read_latest_consensus(db_session, "005930")
    assert view is not None
    target = view["revision"]["목표주가"]
    assert target["current"]["value"] == "458181.82"
    assert {e["column_key"] for e in target["vs_earlier"]} == {
        "VAL2",
        "VAL3",
        "VAL4",
        "VAL5",
    }
