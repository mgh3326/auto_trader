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
from app.models.us_symbol_universe import USSymbolUniverse
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_coverage import (
    CoverageActionability,
    CoverageSourceCandidate,
    InvestCoverageSurface,
)
from app.services.invest_coverage_service import build_invest_coverage


def test_coverage_surface_accepts_source_candidates_and_references_list():
    surface = InvestCoverageSurface(
        surface="investor_flow",
        label="Investor flow",
        state="fresh",
        sourceOfTruth="investor_flow_snapshots",
        references=["toss", "naver"],
        sourceCandidates=[
            CoverageSourceCandidate(
                name="naver_finance",
                surface="investor_flow",
                kind="secondary_source",
                readiness="live",
                latestAt=dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC),
                notes=["naver_finance is one of several wired investor-flow sources"],
            ),
        ],
    )
    assert surface.references == ["toss", "naver"]
    assert surface.sourceCandidates[0].readiness == "live"
    assert surface.sourceCandidates[0].kind == "secondary_source"
    assert surface.actionability.safeByDefault is True
    assert surface.actionability.approvalGates == []


def test_coverage_actionability_rejects_unexpected_fields():
    with pytest.raises(ValueError):
        CoverageActionability(unexpected="run-now")  # type: ignore[call-arg]


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
    # Date KEY stays fixed and intentionally distinct from the shared
    # market-events freshness tests: the global test DB is shared across xdist
    # workers, so reusing 2026-05-11 here can race with tests asserting a
    # completely missing market-events partition on that day.
    trading_day = dt.date(2026, 6, 17)
    # Wall-clock `now`, by contrast, MUST track real time. The news (24h),
    # calendar (36h), holdings (24h) and pending-order (30m) freshness windows in
    # invest_coverage_service compare row timestamps against datetime.now(), not
    # `as_of`. A hardcoded `now` silently rots into a date time-bomb: once the
    # wall-clock date advances past the window, the seeded "fresh" rows fall
    # outside it and the expected-fresh surfaces flip to stale.
    now = dt.datetime.now(dt.UTC)
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
    assert by_surface[("quotes", "kr")].state != "provider_unwired"
    assert by_surface[("ohlcv", "kr")].state == "missing"
    assert by_surface[("valuation_fundamentals", "kr")].state != "provider_unwired"
    assert by_surface[("screener_snapshots", "kr")].actionability.priority == "medium"
    assert (
        by_surface[("screener_snapshots", "kr")].actionability.action
        == "repair_read_model"
    )
    assert (
        by_surface[("orderbook_nxt_capability", "kr")].actionability.priority == "high"
    )
    assert by_surface[("quotes", "kr")].actionability.action in {
        "backfill_candidate",
        "repair_read_model",
    }
    assert by_surface[("quotes", "kr")].actionability.queue == "market-quote-snapshots"
    assert by_surface[("quotes", "kr")].actionability.approvalGates == [
        "production_db_write_approval"
    ]
    assert response.symbols[0].surfaces["screener_snapshots"] == "fresh"
    assert response.symbols[1].surfaces["screener_snapshots"] == "stale"
    assert response.symbols[1].actionability.priority == "high"


@pytest.mark.asyncio
async def test_all_market_symbol_drilldown_resolves_per_symbol_market(db_session):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)
    now_naive = now.replace(tzinfo=None)

    await db_session.execute(
        sa.delete(NewsArticleRelatedSymbol).where(
            NewsArticleRelatedSymbol.id.in_([9720, 9721])
        )
    )
    await db_session.execute(
        sa.delete(NewsArticle).where(NewsArticle.id.in_([9620, 9621]))
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.id.in_([9220, 9221])
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol == "900230")
    )
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol == "ROB203")
    )
    await db_session.commit()

    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900230", name="ROB203 KR", exchange="KOSPI", is_active=True
            ),
            USSymbolUniverse(
                symbol="ROB203", exchange="NASDAQ", name_en="ROB203 US", is_active=True
            ),
            InvestScreenerSnapshot(
                id=9220,
                market="kr",
                symbol="900230",
                snapshot_date=trading_day,
                latest_close=Decimal("1000"),
                closes_window=[1000],
                source="kis",
                computed_at=now,
            ),
            InvestScreenerSnapshot(
                id=9221,
                market="us",
                symbol="ROB203",
                snapshot_date=trading_day,
                latest_close=Decimal("20"),
                closes_window=[20],
                source="yahoo",
                computed_at=now,
            ),
            NewsArticle(
                id=9620,
                url="https://example.com/rob203-kr",
                title="ROB203 KR news",
                source="test",
                feed_source="test",
                market="kr",
                keywords=[],
                article_published_at=now_naive,
                scraped_at=now_naive,
                created_at=now_naive,
            ),
            NewsArticle(
                id=9621,
                url="https://example.com/rob203-us",
                title="ROB203 US news",
                source="test",
                feed_source="test",
                market="us",
                keywords=[],
                article_published_at=now_naive,
                scraped_at=now_naive,
                created_at=now_naive,
            ),
            NewsArticleRelatedSymbol(
                id=9720,
                article_id=9620,
                market="kr",
                symbol="900230",
                source="test",
                created_at=now_naive,
            ),
            NewsArticleRelatedSymbol(
                id=9721,
                article_id=9621,
                market="us",
                symbol="ROB203",
                source="test",
                created_at=now_naive,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session,
        market="all",
        symbols=["900230", "ROB203", "KRW-BTC"],
        as_of=trading_day,
    )

    by_symbol = {row.symbol: row for row in response.symbols}
    assert [row.symbol for row in response.symbols] == ["900230", "ROB203", "KRW-BTC"]
    assert by_symbol["900230"].market == "kr"
    assert by_symbol["ROB203"].market == "us"
    assert by_symbol["KRW-BTC"].market == "crypto"
    assert by_symbol["900230"].surfaces["screener_snapshots"] == "fresh"
    assert by_symbol["ROB203"].surfaces["screener_snapshots"] == "fresh"
    assert by_symbol["KRW-BTC"].actionability.action == "unsupported_no_action"


@pytest.mark.asyncio
async def test_coverage_endpoint_market_all_returns_partitioned_symbol_rows(
    app: FastAPI, db_session
):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)

    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.id == 9320)
    )
    await db_session.commit()
    await db_session.merge(
        KRSymbolUniverse(
            symbol="005930", name="삼성전자", exchange="KOSPI", is_active=True
        )
    )
    await db_session.merge(
        USSymbolUniverse(
            symbol="AAPL", exchange="NASDAQ", name_en="Apple Inc.", is_active=True
        )
    )
    await db_session.merge(
        USSymbolUniverse(
            symbol="MSFT", exchange="NASDAQ", name_en="Microsoft", is_active=True
        )
    )
    db_session.add(
        InvestorFlowSnapshot(
            id=9320,
            market="kr",
            symbol="005930",
            snapshot_date=trading_day,
            source="naver_finance",
            foreign_net=100,
            institution_net=50,
            individual_net=-150,
            collected_at=now,
        )
    )
    await db_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/invest/api/coverage?market=all&symbols=005930,AAPL,MSFT&asOf=2026-05-11"
        )

    assert r.status_code == 200
    payload = r.json()
    assert payload["market"] == "all"
    by_symbol = {row["symbol"]: row for row in payload["symbols"]}
    assert set(by_symbol) == {"005930", "AAPL", "MSFT"}
    assert [row["symbol"] for row in payload["symbols"]] == ["005930", "AAPL", "MSFT"]
    assert by_symbol["005930"]["market"] == "kr"
    assert by_symbol["AAPL"]["market"] == "us"
    assert by_symbol["MSFT"]["market"] == "us"
    assert "naver_investor_flow" in by_symbol["005930"]["surfaces"]
    assert by_symbol["AAPL"]["surfaces"]["investor_flow"] == "unsupported"
    assert by_symbol["MSFT"]["surfaces"]["naver_investor_flow"] == "unsupported"
    assert all("actionability" in row for row in payload["symbols"])
    assert all(
        surface["sourceOfTruth"] != "naver_finance" for surface in payload["surfaces"]
    )


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


@pytest.mark.asyncio
async def test_investor_flow_surface_reports_naver_finance_as_live_candidate(
    db_session,
):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)

    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.id.in_([9310, 9311]))
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900210", "900211"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900210", name="ROB201 NF", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900211", name="ROB201 KIS", exchange="KOSPI", is_active=True
            ),
            InvestorFlowSnapshot(
                id=9310,
                market="kr",
                symbol="900210",
                snapshot_date=trading_day,
                source="naver_finance",
                foreign_net=10,
                institution_net=5,
                individual_net=-15,
                collected_at=now,
            ),
            InvestorFlowSnapshot(
                id=9311,
                market="kr",
                symbol="900211",
                snapshot_date=trading_day,
                source="kis",
                foreign_net=20,
                institution_net=10,
                individual_net=-30,
                collected_at=now,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(db_session, market="kr", as_of=trading_day)
    flow = next(s for s in response.surfaces if s.surface == "investor_flow")

    naver = next((c for c in flow.sourceCandidates if c.name == "naver_finance"), None)
    assert naver is not None, "naver_finance candidate must be present on investor_flow"
    assert naver.kind == "secondary_source"
    assert naver.readiness == "live"
    assert naver.counts is not None
    assert naver.counts.fresh >= 1
    assert flow.sourceOfTruth == "investor_flow_snapshots"
    assert "toss" in flow.references


@pytest.mark.asyncio
async def test_static_naver_candidates_are_attached_to_request_time_surfaces(
    db_session,
):
    response = await build_invest_coverage(
        db_session, market="kr", as_of=dt.date(2026, 5, 11)
    )
    by_surface = {s.surface: s for s in response.surfaces if s.market == "kr"}

    for name in ("valuation_fundamentals", "quotes", "screener_snapshots"):
        nf = next(
            (c for c in by_surface[name].sourceCandidates if c.name == "naver_finance"),
            None,
        )
        assert nf is not None, f"naver_finance candidate missing on {name}"
        assert nf.readiness == "request_time_only"
        assert nf.kind == "candidate"

    research = next(s for s in response.surfaces if s.surface == "research_reports")
    nv = next(
        (c for c in research.sourceCandidates if c.name == "naver_research"), None
    )
    assert nv is not None
    assert nv.readiness == "fixture_backed_poc"
    assert any("Naver" in note for note in response.notes)


@pytest.mark.asyncio
async def test_news_feed_surface_reports_naver_news_candidate_when_articles_exist(
    db_session,
):
    now_naive = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    await db_session.execute(sa.delete(NewsArticle).where(NewsArticle.id == 9610))
    await db_session.commit()
    db_session.add(
        NewsArticle(
            id=9610,
            url="https://finance.naver.com/item/news?code=900201",
            title="ROB201 naver news",
            source="naver_finance",
            feed_source="naver_finance",
            market="kr",
            keywords=[],
            article_published_at=now_naive,
            scraped_at=now_naive,
            created_at=now_naive,
        )
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session, market="kr", as_of=dt.date(2026, 5, 11)
    )
    news = next(s for s in response.surfaces if s.surface == "news_feed")
    nv = next(
        (c for c in news.sourceCandidates if c.name == "naver_finance_news"), None
    )
    assert nv is not None
    assert nv.readiness == "live"
    assert nv.counts is not None
    assert nv.counts.fresh >= 1


@pytest.mark.asyncio
async def test_symbol_rows_expose_naver_investor_flow_state_for_kr(db_session):
    trading_day = dt.date(2026, 5, 11)
    now = dt.datetime(2026, 5, 11, 8, 0, tzinfo=dt.UTC)

    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(InvestorFlowSnapshot.id.in_([9320, 9321]))
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_(["900220", "900221"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol="900220", name="ROB201 NF sym", exchange="KOSPI", is_active=True
            ),
            KRSymbolUniverse(
                symbol="900221", name="ROB201 no-NF", exchange="KOSPI", is_active=True
            ),
            InvestorFlowSnapshot(
                id=9320,
                market="kr",
                symbol="900220",
                snapshot_date=trading_day,
                source="naver_finance",
                foreign_net=1,
                institution_net=1,
                individual_net=-2,
                collected_at=now,
            ),
            InvestorFlowSnapshot(
                id=9321,
                market="kr",
                symbol="900221",
                snapshot_date=trading_day,
                source="kis",
                foreign_net=1,
                institution_net=1,
                individual_net=-2,
                collected_at=now,
            ),
        ]
    )
    await db_session.commit()

    response = await build_invest_coverage(
        db_session,
        market="kr",
        symbols=["900220", "900221"],
        as_of=trading_day,
    )
    by_symbol = {row.symbol: row for row in response.symbols}
    assert by_symbol["900220"].surfaces["naver_investor_flow"] == "fresh"
    assert by_symbol["900221"].surfaces["naver_investor_flow"] == "missing"


@pytest.mark.asyncio
async def test_coverage_endpoint_exposes_naver_candidates_field_on_kr(
    app: FastAPI, db_session
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/invest/api/coverage?market=kr")
    assert r.status_code == 200
    payload = r.json()

    flat_candidates = [
        candidate
        for surface in payload["surfaces"]
        for candidate in surface.get("sourceCandidates", [])
    ]
    naver_names = {candidate["name"] for candidate in flat_candidates}
    assert "naver_finance" in naver_names

    for surface in payload["surfaces"]:
        assert isinstance(surface.get("references"), list)
        assert "toss" in surface["references"]
        assert surface["sourceOfTruth"] != "naver_finance"

    assert any("Naver" in note for note in payload["notes"])
