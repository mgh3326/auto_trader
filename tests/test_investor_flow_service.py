from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.services.invest_view_model import investor_flow_service as flow_service
from app.services.invest_view_model.investor_flow_service import (
    build_investor_flow_cards,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
    InvestorFlowSnapshotUpsert,
)


@pytest.fixture
def app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_build_investor_flow_cards_empty_symbols_returns_empty(db_session):
    response = await build_investor_flow_cards(
        db=db_session,
        symbols=[],
        market="kr",
        as_of=dt.date(2026, 5, 11),
    )

    assert response.market == "kr"
    assert response.dataState == "empty"
    assert response.items == []


@pytest.mark.asyncio
async def test_build_investor_flow_cards_marks_missing_symbol(db_session):
    missing_symbol = "989898"
    response = await build_investor_flow_cards(
        db=db_session,
        symbols=[missing_symbol],
        market="kr",
        as_of=dt.date(2026, 5, 11),
    )

    assert response.dataState == "missing"
    assert response.items[0].symbol == missing_symbol
    assert response.items[0].dataState == "missing"
    assert response.items[0].source is None


def test_resolve_investor_flow_as_of_defaults_to_previous_kr_session(monkeypatch):
    calls: list[tuple[str, dt.date]] = []

    def fake_previous_session(market: str, day: dt.date) -> dt.date | None:
        calls.append((market, day))
        return dt.date(2026, 6, 12)

    monkeypatch.setattr(
        flow_service, "previous_trading_session", fake_previous_session
    )

    now = dt.datetime(2026, 6, 15, 0, 5, tzinfo=dt.UTC)  # Mon 09:05 KST

    assert flow_service._resolve_investor_flow_as_of(None, now=now) == dt.date(
        2026, 6, 12
    )
    assert calls == [("kr", dt.date(2026, 6, 15))]


def test_resolve_investor_flow_as_of_keeps_explicit_effective_date(monkeypatch):
    def fail_if_called(market: str, day: dt.date) -> dt.date | None:
        raise AssertionError(f"unexpected calendar lookup: {market} {day}")

    monkeypatch.setattr(flow_service, "previous_trading_session", fail_if_called)

    assert flow_service._resolve_investor_flow_as_of(
        dt.date(2026, 5, 11)
    ) == dt.date(2026, 5, 11)


@pytest.mark.asyncio
async def test_build_investor_flow_cards_uses_previous_kr_session_by_default(
    db_session, monkeypatch
):
    repo = InvestorFlowSnapshotsRepository(db_session)
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900198",
            snapshot_date=dt.date(2026, 6, 12),
            foreign_net=10,
            institution_net=20,
            individual_net=-30,
            source="naver_finance",
            collected_at=dt.datetime(2026, 6, 12, 7, 0, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        flow_service,
        "_resolve_investor_flow_as_of",
        lambda as_of=None, *, now=None: dt.date(2026, 6, 12),
    )

    response = await flow_service.build_investor_flow_cards(
        db=db_session,
        symbols=["900198"],
        market="kr",
        max_stale_days=1,
    )

    assert response.asOf == dt.date(2026, 6, 12)
    assert response.dataState == "fresh"
    assert response.items[0].dataState == "fresh"


@pytest.mark.asyncio
async def test_build_investor_flow_cards_marks_all_stale(db_session):
    repo = InvestorFlowSnapshotsRepository(db_session)
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900197",
            snapshot_date=dt.date(2026, 5, 6),
            foreign_net=-400,
            institution_net=0,
            individual_net=400,
            source="naver_finance",
            collected_at=dt.datetime(2026, 5, 6, 7, 0, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    response = await build_investor_flow_cards(
        db=db_session,
        symbols=["900197"],
        market="kr",
        as_of=dt.date(2026, 5, 11),
        max_stale_days=1,
    )

    assert response.dataState == "stale"
    assert response.items[0].dataState == "stale"
    assert response.items[0].snapshotDate == dt.date(2026, 5, 6)


@pytest.mark.asyncio
async def test_build_investor_flow_cards_marks_stale_and_fresh(db_session):
    repo = InvestorFlowSnapshotsRepository(db_session)
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900194",
            snapshot_date=dt.date(2026, 5, 7),
            foreign_net=-1_000,
            institution_net=-2_000,
            individual_net=3_000,
            foreign_net_sell_rank=4,
            institution_net_sell_rank=8,
            foreign_consecutive_sell_days=2,
            source="naver_finance",
            collected_at=dt.datetime(2026, 5, 7, 7, 0, tzinfo=dt.UTC),
        )
    )
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900195",
            snapshot_date=dt.date(2026, 5, 11),
            foreign_net=5_000,
            institution_net=7_000,
            individual_net=-12_000,
            foreign_net_buy_rank=1,
            institution_net_buy_rank=2,
            foreign_consecutive_buy_days=5,
            institution_consecutive_buy_days=3,
            source="naver_finance",
            collected_at=dt.datetime(2026, 5, 11, 7, 0, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    response = await build_investor_flow_cards(
        db=db_session,
        symbols=["900194", "900195"],
        market="kr",
        as_of=dt.date(2026, 5, 11),
        max_stale_days=1,
    )

    assert response.dataState == "partial"
    stale, fresh = response.items
    assert stale.dataState == "stale"
    assert stale.doubleSell is True
    assert stale.foreignNetSellRank == 4
    assert stale.foreignConsecutiveSellDays == 2
    assert fresh.dataState == "fresh"
    assert fresh.doubleBuy is True
    assert fresh.foreignNetBuyRank == 1
    assert fresh.institutionNetBuyRank == 2
    assert fresh.foreignConsecutiveBuyDays == 5


@pytest.mark.asyncio
async def test_investor_flow_endpoint_returns_read_only_view_model(
    app: FastAPI, db_session
):
    repo = InvestorFlowSnapshotsRepository(db_session)
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900196",
            snapshot_date=dt.date.today(),
            foreign_net=10,
            institution_net=20,
            individual_net=-30,
            source="naver_finance",
        )
    )
    await db_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/invest/api/investor-flow?symbols=900196&market=kr")

    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "kr"
    assert body["dataState"] == "fresh"
    assert body["items"][0]["symbol"] == "900196"
    assert body["items"][0]["foreignNet"] == 10
    assert body["items"][0]["doubleBuy"] is True
