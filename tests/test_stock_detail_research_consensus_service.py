from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_stock_detail_research_consensus import (
    StockDetailResearchConsensusResponse,
    StockDetailResearchFreshness,
)
from app.schemas.research_reports import (
    ResearchReportCitation,
    ResearchReportsReadinessResponse,
)
from app.services.invest_view_model.stock_detail_research_consensus_service import (
    StockDetailResearchConsensusProviders,
    build_stock_detail_research_consensus,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol


async def _resolve_kr(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db="005930",
        display_name="삼성전자",
        exchange="KOSPI",
        instrument_type="equity_kr",
        asset_type="equity",
        asset_category="kr_stock",
        currency="KRW",
    )


async def _resolve_us(market, raw_symbol, db):
    return ResolvedSymbol(
        symbol_db="QQQM",
        display_name="Invesco NASDAQ 100 ETF",
        exchange="NASDAQ",
        instrument_type="etf_us",
        asset_type="etf",
        asset_category="us_etf",
        currency="USD",
    )


@pytest.mark.asyncio
async def test_build_stock_detail_research_consensus_combines_opinions_and_citations_without_body_fields():
    now = datetime.now(UTC)

    async def opinions_provider(symbol, market, limit):
        return {
            "source": "naver",
            "current_price": 70000,
            "opinions": [
                {
                    "firm": "A증권",
                    "rating": "매수",
                    "target_price": 84000,
                    "date": (now - timedelta(days=20)).date().isoformat(),
                },
                {
                    "firm": "B증권",
                    "rating": "중립",
                    "target_price": 72000,
                    "date": (now - timedelta(days=21)).date().isoformat(),
                },
            ],
        }

    async def citations_provider(db, symbol, limit):
        return [
            ResearchReportCitation(
                source="naver_research",
                title="삼성전자 실적 프리뷰",
                analyst="홍길동",
                published_at=now,
                excerpt="메모리 회복과 AI 수요를 점검합니다.",
                detail_url="https://example.com/reports/1",
                symbol_candidates=[
                    {"symbol": "005930", "market": "kr", "source": "ticker"}
                ],
                attribution_publisher="Naver",
                attribution_copyright_notice="copyright",
            )
        ]

    async def readiness_provider(db, source, max_age_hours):
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=True,
            is_stale=False,
            latest_run_uuid="run-1",
            latest_finished_at=now,
            latest_inserted_count=1,
            latest_skipped_count=0,
            latest_report_count=1,
            warnings=[],
            max_age_hours=max_age_hours,
        )

    response = await build_stock_detail_research_consensus(
        market="kr",
        symbol="005930",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_kr,
            opinions=opinions_provider,
            citations=citations_provider,
            readiness=readiness_provider,
        ),
    )

    assert response.market == "kr"
    assert response.symbol == "005930"
    assert response.state == "ready"
    assert response.dataState == "fresh"
    assert response.emptyReason is None
    assert response.sourceOfTruth == "analyst_opinions_and_research_reports"
    assert response.consensus is not None
    assert response.consensus.buyCount == 1
    assert response.consensus.holdCount == 1
    assert response.consensus.totalCount == 2
    assert response.consensus.avgTargetPrice == 78000
    assert response.consensus.upsidePct == pytest.approx(11.43)
    assert response.citations[0].title == "삼성전자 실적 프리뷰"

    dumped = response.model_dump(by_alias=True)
    forbidden = {
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "raw_payload",
        "raw_payload_json",
    }
    assert forbidden.isdisjoint(str(dumped).lower())


@pytest.mark.asyncio
async def test_build_stock_detail_research_consensus_keeps_citations_when_opinions_missing():
    now = datetime.now(UTC)

    async def opinions_provider(symbol, market, limit):
        return {"error": True, "message": "provider unavailable"}

    async def citations_provider(db, symbol, limit):
        return [
            ResearchReportCitation(
                source="issuer_ir",
                title="QQQM holdings note",
                published_at=now,
                excerpt="ETF 구성 종목 변경만 요약합니다.",
            )
        ]

    async def readiness_provider(db, source, max_age_hours):
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=False,
            is_stale=True,
            latest_run_uuid="run-stale",
            latest_finished_at=now - timedelta(days=3),
            latest_inserted_count=1,
            latest_skipped_count=0,
            latest_report_count=1,
            warnings=["research_reports_stale"],
            max_age_hours=max_age_hours,
        )

    response = await build_stock_detail_research_consensus(
        market="us",
        symbol="QQQM",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_us,
            opinions=opinions_provider,
            citations=citations_provider,
            readiness=readiness_provider,
        ),
    )

    assert response.state == "partial"
    assert response.dataState == "stale"
    assert response.emptyReason is None
    assert response.consensus is None
    assert response.citations[0].source == "issuer_ir"
    assert "analyst_opinions_unavailable" in response.warnings
    assert "research_reports_stale" in response.warnings


@pytest.mark.asyncio
async def test_build_stock_detail_research_consensus_reports_missing_when_no_sources():
    async def opinions_provider(symbol, market, limit):
        return {"opinions": [], "source": "yfinance"}

    async def citations_provider(db, symbol, limit):
        return []

    async def readiness_provider(db, source, max_age_hours):
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=False,
            is_stale=False,
            latest_inserted_count=0,
            latest_skipped_count=0,
            latest_report_count=0,
            warnings=[],
            max_age_hours=max_age_hours,
        )

    response = await build_stock_detail_research_consensus(
        market="us",
        symbol="QQQM",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_us,
            opinions=opinions_provider,
            citations=citations_provider,
            readiness=readiness_provider,
        ),
    )

    assert response.state == "missing"
    assert response.dataState == "missing"
    assert response.emptyReason == "no_analyst_consensus_or_research_reports"
    assert response.consensus is None
    assert response.citations == []


@pytest.mark.asyncio
async def test_build_stock_detail_research_consensus_reports_error_state_when_providers_fail():
    async def failing_opinions_provider(symbol, market, limit):
        raise RuntimeError("opinions offline")

    async def failing_citations_provider(db, symbol, limit):
        raise RuntimeError("research db offline")

    async def failing_readiness_provider(db, source, max_age_hours):
        raise RuntimeError("freshness offline")

    response = await build_stock_detail_research_consensus(
        market="us",
        symbol="QQQM",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_us,
            opinions=failing_opinions_provider,
            citations=failing_citations_provider,
            readiness=failing_readiness_provider,
        ),
    )

    assert response.state == "missing"
    assert response.dataState == "error"
    assert response.emptyReason == "provider_error"
    assert response.sourceOfTruth == "none"
    assert response.consensus is None
    assert response.citations == []
    assert "analyst_opinions_unavailable" in response.warnings
    assert "research_reports_unavailable" in response.warnings
    assert "research_reports_readiness_unavailable" in response.warnings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    async def _stub_research_consensus(**kwargs):
        assert kwargs["market"] == "us"
        assert kwargs["symbol"] == "QQQM"
        return StockDetailResearchConsensusResponse(
            symbol="QQQM",
            market="us",
            displayName="Invesco NASDAQ 100 ETF",
            state="ready",
            dataState="fresh",
            warnings=[],
            sourceOfTruth="analyst_opinions_and_research_reports",
            asOf=datetime(2026, 5, 10, 9, 31, tzinfo=UTC),
            consensus=None,
            citations=[ResearchReportCitation(source="issuer_ir", title="QQQM note")],
            freshness=StockDetailResearchFreshness(
                isReady=True,
                isStale=False,
                latestRunUuid="run-1",
                latestFinishedAt=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
                latestReportCount=1,
                maxAgeHours=24,
            ),
        )

    monkeypatch.setattr(
        invest_api, "build_stock_detail_research_consensus", _stub_research_consensus
    )
    return TestClient(app)


@pytest.mark.unit
def test_research_consensus_route_returns_read_only_contract(
    client: TestClient,
) -> None:
    response = client.get("/invest/api/stock-detail/us/QQQM/research-consensus")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "QQQM"
    assert body["market"] == "us"
    assert body["state"] == "ready"
    assert body["dataState"] == "fresh"
    assert body["sourceOfTruth"] == "analyst_opinions_and_research_reports"
    assert body["citations"][0]["title"] == "QQQM note"
    forbidden = {"pdf_body", "pdf_text", "extracted_text", "full_text", "raw_payload"}
    assert forbidden.isdisjoint(str(body).lower())


@pytest.mark.unit
def test_research_consensus_route_rejects_crypto(client: TestClient) -> None:
    response = client.get("/invest/api/stock-detail/crypto/KRW-BTC/research-consensus")

    assert response.status_code == 400
    assert response.json()["detail"] == "research_consensus_supports_kr_us_only"


@pytest.mark.unit
def test_research_consensus_route_maps_symbol_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.invest_view_model.stock_detail_symbol_resolver import (
        SymbolNotFound,
    )

    async def _raise_not_found(**kwargs):
        raise SymbolNotFound("missing")

    monkeypatch.setattr(
        invest_api, "build_stock_detail_research_consensus", _raise_not_found
    )

    response = client.get("/invest/api/stock-detail/us/MISSING/research-consensus")

    assert response.status_code == 404
    assert response.json()["detail"] == "symbol_not_found"


@pytest.mark.asyncio
async def test_stock_detail_consensus_applies_recency_window_like_tool():
    """ROB-486: 패널이 행별 date 를 보존해 도구와 동일한 윈도우 집계를 탄다 (005880 모양)."""
    now = datetime.now(UTC)
    recent = (now - timedelta(days=23)).date().isoformat()
    stale = (now - timedelta(days=2050)).date().isoformat()

    async def opinions_provider(symbol, market, limit):
        return {
            "source": "naver",
            "current_price": 1914,
            "opinions": [
                {
                    "firm": "신한투자증권",
                    "rating": "매수",
                    "target_price": 3000,
                    "date": recent,
                },
                {
                    "firm": "하나증권",
                    "rating": "매수",
                    "target_price": 23000,
                    "date": stale,
                },
            ],
        }

    async def citations_provider(db, symbol, limit):
        return []

    async def readiness_provider(db, source, max_age_hours):
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=True,
            is_stale=False,
            latest_inserted_count=0,
            latest_skipped_count=0,
            latest_report_count=0,
            warnings=[],
            max_age_hours=max_age_hours,
        )

    response = await build_stock_detail_research_consensus(
        market="kr",
        symbol="005880",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_kr,
            opinions=opinions_provider,
            citations=citations_provider,
            readiness=readiness_provider,
        ),
    )

    assert response.consensus is not None
    assert response.consensus.totalCount == 1
    assert response.consensus.buyCount == 1
    assert response.consensus.avgTargetPrice == 3000
    assert response.consensus.upsidePct == pytest.approx(56.74, abs=0.01)


@pytest.mark.asyncio
async def test_stock_detail_consensus_stale_only_reports_missing():
    """ROB-486 (031330 모양): 윈도우 생존 row 0 → 패널 consensus 미노출 (폴백 금지)."""

    async def opinions_provider(symbol, market, limit):
        return {
            "source": "naver",
            "current_price": 15360,
            "opinions": [
                {
                    "firm": "한국기업데이터",
                    "rating": "중립",
                    "target_price": None,
                    "date": "2019-12-27",
                },
                {
                    "firm": "대신증권",
                    "rating": "매수",
                    "target_price": 2700,
                    "date": "2015-08-24",
                },
            ],
        }

    async def citations_provider(db, symbol, limit):
        return []

    async def readiness_provider(db, source, max_age_hours):
        return ResearchReportsReadinessResponse(
            source=source,
            is_ready=False,
            is_stale=False,
            latest_inserted_count=0,
            latest_skipped_count=0,
            latest_report_count=0,
            warnings=[],
            max_age_hours=max_age_hours,
        )

    response = await build_stock_detail_research_consensus(
        market="kr",
        symbol="031330",
        db=SimpleNamespace(),
        providers=StockDetailResearchConsensusProviders(
            resolver=_resolve_kr,
            opinions=opinions_provider,
            citations=citations_provider,
            readiness=readiness_provider,
        ),
    )

    assert response.consensus is None
    assert response.state == "missing"
    assert response.emptyReason == "no_analyst_consensus_or_research_reports"
