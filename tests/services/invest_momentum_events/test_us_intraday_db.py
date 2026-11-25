"""hk 6510: US snapshot rows, status rows and first captures (CI database)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.models.invest_momentum_event_snapshot import (
    InvestMomentumEventRun,
    InvestMomentumEventSnapshot,
)
from app.services.invest_momentum_events import intraday, us_intraday
from app.services.invest_momentum_events.models import MomentumEventUpsert
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DAY = dt.date(2026, 11, 25)
T1 = dt.datetime(2026, 11, 25, 9, 30, 0, tzinfo=dt.UTC)  # 04:30 ET, pre-market
T2 = T1 + dt.timedelta(minutes=5)


def _us_row(symbol: str, *, snapshot_at=T1, side="down", rank=1, **extra):
    return MomentumEventUpsert(
        snapshot_at=snapshot_at,
        trading_date=DAY,
        source="kis_overseas_ranking",
        surface=f"us:kis_overseas_ranking:us_pre:{side}",
        market="us",
        trade_type="US",
        market_type="NAS",
        order_type=side,
        rank=rank,
        symbol=symbol,
        asset_type="common_stock",
        first_received_at=snapshot_at,
        field_reasons={"trade_value": "not_in_payload"},
        raw_payload={"trade_value_basis": "price_times_volume"},
        **extra,
    )


async def _clean(session) -> None:
    await session.execute(
        delete(InvestMomentumEventSnapshot).where(
            InvestMomentumEventSnapshot.trading_date == DAY
        )
    )
    await session.execute(
        delete(InvestMomentumEventRun).where(InvestMomentumEventRun.trading_date == DAY)
    )
    await session.commit()


async def test_us_row_round_trips_and_a_replay_changes_nothing(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    first = _us_row(
        "DELL",
        price=Decimal("118.42"),
        change_rate=Decimal("-7.45"),
        volume=604000,
        trade_value=Decimal("71525680"),
    )
    assert await repo.insert_momentum(first) is True
    changed = first.model_copy(update={"rank": 9, "trade_value": Decimal("1")})
    assert await repo.insert_momentum(changed) is False
    await db_session.commit()

    row = (
        await db_session.execute(
            select(InvestMomentumEventSnapshot).where(
                InvestMomentumEventSnapshot.symbol == "DELL",
                InvestMomentumEventSnapshot.snapshot_at == T1,
            )
        )
    ).scalar_one()
    assert row.market == "us"
    assert row.source == "kis_overseas_ranking"
    assert row.surface == "us:kis_overseas_ranking:us_pre:down"
    assert row.asset_type == "common_stock"
    assert row.first_received_at == T1
    assert row.field_reasons == {"trade_value": "not_in_payload"}
    assert row.raw_payload == {"trade_value_basis": "price_times_volume"}
    assert row.trade_value == Decimal("71525680")
    await _clean(db_session)


async def test_closed_market_and_source_vocabularies_are_enforced(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    with pytest.raises(IntegrityError):
        await repo.insert_momentum(_us_row("URBN").model_copy(update={"market": "jp"}))
    await db_session.rollback()
    with pytest.raises(IntegrityError):
        await repo.insert_momentum(
            _us_row("URBN").model_copy(update={"source": "benzinga"})
        )
    await db_session.rollback()
    for source in ("alpaca_screener", "naver_stock"):
        assert (
            await repo.insert_momentum(
                _us_row(f"X{source[:3].upper()}", extra=None).model_copy(
                    update={"source": source}
                )
            )
            is True
        )
    await _clean(db_session)


async def test_source_unavailable_is_a_recordable_run_reason(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    created = await repo.insert_run_status(
        run_id=us_intraday.run_id_for(T1),
        snapshot_at=T1,
        trading_date=DAY,
        surface=us_intraday.surface_key("alpaca_screener", "us_pre", "up"),
        status="error",
        reason="source_unavailable",
        row_count=0,
    )
    assert created is True
    await db_session.commit()
    await _clean(db_session)


async def test_schema_shape_matches_the_migration(db_session) -> None:
    columns = {
        str(row[0])
        for row in (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'invest_momentum_event_snapshots'"
                )
            )
        ).all()
    }
    assert {"asset_type", "first_received_at", "field_reasons"} <= columns
    indexes = {
        str(row[0])
        for row in (
            await db_session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'invest_momentum_event_snapshots'"
                )
            )
        ).all()
    }
    assert "ix_invest_momentum_event_snapshots_market_date_symbol" in indexes


async def test_an_unknown_run_reason_is_rejected(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    with pytest.raises(IntegrityError):
        await repo.insert_run_status(
            run_id=us_intraday.run_id_for(T1),
            snapshot_at=T1,
            trading_date=DAY,
            surface=us_intraday.surface_key("alpaca_screener", "us_pre", "up"),
            status="error",
            reason="made_up_reason",
            row_count=0,
        )
    await db_session.rollback()
    await _clean(db_session)


async def test_first_captures_is_us_scoped_and_keeps_the_stored_surface(
    db_session,
) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    await repo.insert_momentum(_us_row("DELL", rank=3))
    await repo.insert_momentum(_us_row("URBN", rank=1, snapshot_at=T2))
    # a KR row of the same trading date must not leak into the US answer
    await repo.insert_momentum(
        MomentumEventUpsert(
            snapshot_at=T1,
            trading_date=DAY,
            surface="domestic_market_stock_default",
            market="kr",
            trade_type="KRX",
            market_type="ALL",
            order_type="up",
            rank=1,
            symbol="005930",
        )
    )
    await db_session.commit()

    captures = await intraday.first_captures(db_session, DAY, market="us")
    assert [c.symbol for c in captures] == ["DELL", "URBN"]
    assert captures[0].surface == "us:kis_overseas_ranking:us_pre:down"
    assert captures[0].rank == 3
    await _clean(db_session)
