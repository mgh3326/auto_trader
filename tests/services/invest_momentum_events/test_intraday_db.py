"""hk 6154: insert-only snapshots, status rows and first captures (CI database)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models.invest_momentum_event_snapshot import (
    InvestMomentumEventRun,
    InvestMomentumEventSnapshot,
)
from app.services.invest_momentum_events import intraday
from app.services.invest_momentum_events.models import MomentumEventUpsert
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DAY = dt.date(2026, 11, 24)
T1 = dt.datetime(2026, 11, 24, 1, 0, 0, tzinfo=dt.UTC)  # 10:00 KST
T2 = T1 + dt.timedelta(minutes=5)
ROW = {
    "itemcode": "005930",
    "itemname": "삼성전자",
    "nowPrice": "78500",
    "prevChangeRate": "1.5",
}


class Fetcher:
    def __init__(self, by_order):
        self.by_order = by_order

    async def fetch_domestic_stock_default(self, **kwargs):
        outcome = self.by_order[kwargs["order_type"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


def _payload(snapshot_at, symbol, order_type="up", rank=1, **extra):
    return MomentumEventUpsert(
        snapshot_at=snapshot_at,
        trading_date=DAY,
        surface="domestic_market_stock_default",
        trade_type="KRX",
        market_type="ALL",
        order_type=order_type,
        rank=rank,
        symbol=symbol,
        **extra,
    )


async def test_same_snapshot_twice_keeps_row_count_and_values(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    first = _payload(T1, "005930", rank=1, price=Decimal("78500"), name="삼성전자")
    assert await repo.insert_momentum(first) is True
    changed = first.model_copy(
        update={"rank": 9, "price": Decimal("1"), "name": "changed"}
    )
    assert await repo.insert_momentum(changed) is False
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(InvestMomentumEventSnapshot).where(
                    InvestMomentumEventSnapshot.trading_date == DAY
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert (rows[0].rank, rows[0].price, rows[0].name) == (
        1,
        Decimal("78500.000000"),
        "삼성전자",
    )
    await _clean(db_session)


async def test_run_error_empty_ok_write_the_right_rows(db_session) -> None:
    await _clean(db_session)
    import httpx

    request = httpx.Request("GET", "https://stock.naver.com/x")
    boom = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(503, request=request)
    )
    fetcher = Fetcher(
        {"up": boom, "down": [], "priceTop": [ROW, {**ROW, "itemcode": "000660"}]}
    )
    now = dt.datetime(2026, 11, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    report = await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=db_session,
        now=now,
        commit=True,
        is_session_day=lambda day: True,
    )
    assert report.status == "recorded"

    runs = {
        r.surface: r
        for r in (
            await db_session.execute(
                select(InvestMomentumEventRun).where(
                    InvestMomentumEventRun.trading_date == DAY
                )
            )
        )
        .scalars()
        .all()
    }
    assert (runs["stock:KRX:ALL:up"].status, runs["stock:KRX:ALL:up"].reason) == (
        "error",
        "http_5xx",
    )
    assert runs["stock:KRX:ALL:up"].row_count == 0
    assert (runs["stock:KRX:ALL:down"].status, runs["stock:KRX:ALL:down"].reason) == (
        "empty",
        "no_rows",
    )
    assert runs["stock:KRX:ALL:priceTop"].status == "ok"
    assert runs["stock:KRX:ALL:priceTop"].row_count == 2
    assert len({r.run_id for r in runs.values()}) == 1

    by_order = dict(
        (
            await db_session.execute(
                select(
                    InvestMomentumEventSnapshot.order_type,
                    func.count(),
                )
                .where(InvestMomentumEventSnapshot.trading_date == DAY)
                .group_by(InvestMomentumEventSnapshot.order_type)
            )
        ).all()
    )
    assert by_order == {"priceTop": 2}  # error and empty surfaces wrote no rows

    # same snapshot again: nothing changes
    again = await intraday.run_intraday_movers(
        fetcher=fetcher,
        session=db_session,
        now=now,
        commit=True,
        is_session_day=lambda day: True,
    )
    assert all(s.already_recorded for s in again.surfaces)
    total = (
        await db_session.execute(
            select(func.count())
            .select_from(InvestMomentumEventSnapshot)
            .where(InvestMomentumEventSnapshot.trading_date == DAY)
        )
    ).scalar_one()
    assert total == 2
    await _clean(db_session)


async def test_status_check_constraint_rejects_inconsistent_rows(db_session) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    import uuid

    with pytest.raises(IntegrityError):
        await repo.insert_run_status(
            run_id=uuid.uuid4(),
            snapshot_at=T1,
            trading_date=DAY,
            surface="stock:KRX:ALL:up",
            status="error",
            reason="made up reason",
            row_count=0,
        )
    await db_session.rollback()
    with pytest.raises(IntegrityError):
        await repo.insert_run_status(
            run_id=uuid.uuid4(),
            snapshot_at=T1,
            trading_date=DAY,
            surface="stock:KRX:ALL:up",
            status="ok",
            reason=None,
            row_count=0,
        )
    await db_session.rollback()


async def test_first_captures_returns_the_earliest_appearance_per_symbol(
    db_session,
) -> None:
    await _clean(db_session)
    repo = InvestMomentumEventSnapshotsRepository(db_session)
    await repo.insert_momentum(
        _payload(
            T2, "005930", "up", 1, change_rate=Decimal("9"), trade_value=Decimal("5")
        )
    )
    await repo.insert_momentum(
        _payload(
            T1,
            "005930",
            "priceTop",
            7,
            change_rate=Decimal("3"),
            trade_value=Decimal("8"),
        )
    )
    await repo.insert_momentum(
        _payload(T2, "000660", "down", 2, change_rate=Decimal("-4"))
    )
    # tie on snapshot_at: best rank wins, then order_type name
    await repo.insert_momentum(_payload(T2, "000660", "up", 5))
    await repo.insert_momentum(_payload(T2, "035420", "down", 3))
    await repo.insert_momentum(_payload(T2, "035420", "priceTop", 3))
    await db_session.commit()

    captures = await intraday.first_captures(db_session, DAY)
    assert [(c.symbol, c.snapshot_at, c.surface, c.rank) for c in captures] == [
        ("005930", T1, "stock:KRX:ALL:priceTop", 7),
        ("000660", T2, "stock:KRX:ALL:down", 2),
        ("035420", T2, "stock:KRX:ALL:down", 3),
    ]
    assert captures[0].change_rate == Decimal("3.0000")
    assert captures[0].trade_value == Decimal("8.00")
    assert await intraday.first_captures(db_session, DAY + dt.timedelta(days=1)) == []
    again = await intraday.first_captures(db_session, DAY)
    assert again == captures  # deterministic
    await _clean(db_session)
