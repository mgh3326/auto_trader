from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_events import MarketEventIngestionPartition
from app.models.news import NewsArticle, NewsArticleRelatedSymbol, NewsIngestionRun
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.services.invest_coverage_service import build_invest_coverage


@pytest.fixture
def app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_build_invest_coverage_reports_fresh_partial_and_provider_unwired(
    db_session,
):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)
    now_naive = now.replace(tzinfo=None)

    await db_session.execute(
        sa.delete(NewsArticleRelatedSymbol).where(NewsArticleRelatedSymbol.id == 9701)
    )
    await db_session.execute(sa.delete(NewsArticle).where(NewsArticle.id == 9601))
    await db_session.execute(
        sa.delete(NewsIngestionRun).where(NewsIngestionRun.id == 9501)
    )
    await db_session.execute(
        sa.delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.id == 9401
        )
    )
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.id == 9301)
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.id.in_([9201, 9202])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900201", "900202"])
        )
    )
    await db_session.commit()

    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900201", name="ROB192 Fresh", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900202", name="ROB192 Stale", exchange="KOSPI", is_active=True
            ),
            InvestScreenerSnapshot(
                id=9201,
                market="kr",
                symbol="900201",
                snapshot_date=trading_day,
                latest_close=Decimal("1000"),
                closes_window=[1000],
                source="kis",
                computed_at=now,
            ),
            InvestScreenerSnapshot(
                id=9202,
                market="kr",
                symbol="900202",
                snapshot_date=dt.date(2026, 5, 8),
                latest_close=Decimal("900"),
                closes_window=[900],
                source="kis",
                computed_at=now - dt.timedelta(days=3),
            ),
            InvestorFlowSnapshot(
                id=9301,
                market="kr",
                symbol="900201",
                snapshot_date=trading_day,
                source="naver_finance",
                foreign_net=100,
                institution_net=50,
                individual_net=-150,
                collected_at=now,
            ),
            MarketEventIngestionPartition(
                id=9401,
                source="test",
                category="economic",
                market="kr",
                partition_date=trading_day,
                status="success",
                event_count=1,
                finished_at=now,
            ),
            NewsIngestionRun(
                id=9501,
                run_uuid="rob192-news-run",
                market="kr",
                feed_set="test",
                started_at=now_naive,
                finished_at=now_naive,
                status="success",
                source_counts={"test": 1},
                inserted_count=1,
                skipped_count=0,
                created_at=now_naive,
            ),
            NewsArticle(
                id=9601,
                url="https://example.com/rob192-news",
                title="ROB192 news",
                source="test",
                feed_source="test",
                market="kr",
                keywords=[],
                article_published_at=now_naive,
                scraped_at=now_naive,
                created_at=now_naive,
            ),
            NewsArticleRelatedSymbol(
                id=9701,
                article_id=9601,
                market="kr",
                symbol="900201",
                source="test",
                created_at=now_naive,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session,
        market="kr",
        symbols=["900201", "900202"],
        as_of=trading_day,
    )

    by_surface = {
        (surface.surface, surface.market): surface for surface in response.surfaces
    }
    assert by_surface[("screener_snapshots", "kr")].state == "partial"
    assert by_surface[("investor_flow", "kr")].state == "partial"
    assert by_surface[("news_feed", "kr")].state == "fresh"
    assert by_surface[("calendar_events", "kr")].state == "fresh"
    assert by_surface[("holdings", "kr")].state == "fresh"
    assert by_surface[("pending_orders", "kr")].state == "fresh"
    assert by_surface[("orderbook_nxt_capability", "kr")].state == "missing"
    assert by_surface[("quotes", "kr")].state == "provider_unwired"
    assert by_surface[("ohlcv", "kr")].state == "provider_unwired"
    assert response.symbols[0].surfaces["screener_snapshots"] == "fresh"
    assert response.symbols[1].surfaces["screener_snapshots"] == "stale"


@pytest.mark.asyncio
async def test_invest_coverage_endpoint_is_read_only_and_exposes_gaps(
    app: FastAPI, db_session
):
    before = len(db_session.new)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/invest/api/coverage?market=crypto&symbols=KRW-BTC")

    assert r.status_code == 200
    payload = r.json()
    assert payload["market"] == "crypto"
    assert "provider_unwired" in payload["states"]
    assert any(
        surface["state"] in {"unsupported", "provider_unwired"}
        for surface in payload["surfaces"]
    )
    assert len(db_session.new) == before
