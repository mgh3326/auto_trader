"""Router tests for GET /invest/api/feed/research (ROB-179)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_UNSET = object()


def _make_app(held=None, watch=None):
    from app.core.db import get_db
    from app.routers.dependencies import get_authenticated_user
    from app.routers.invest_api import get_invest_home_service
    from app.routers.invest_api import router as invest_router
    from app.schemas.invest_home import (
        Account,
        Holding,
        InvestHomeResponse,
        InvestHomeResponseMeta,
    )
    from app.services.invest_home_service import (
        build_grouped_holdings,
        build_home_summary,
    )

    class _StubService:
        def __init__(self, holdings=None):
            self._holdings = holdings or []

        async def get_home(self, *, user_id: int, **kwargs) -> InvestHomeResponse:
            accounts: list[Account] = []
            return InvestHomeResponse(
                homeSummary=build_home_summary(accounts),
                accounts=accounts,
                holdings=self._holdings,
                groupedHoldings=build_grouped_holdings(self._holdings),
                meta=InvestHomeResponseMeta(warnings=[]),
            )

    stub_holdings: list[Holding] = []
    if held:
        for mkt, sym in held:
            stub_holdings.append(
                Holding(
                    holdingId=f"h-{sym}",
                    accountId="a1",
                    source="kis",
                    accountKind="live",
                    symbol=sym,
                    market=mkt.upper(),
                    assetType="equity",
                    assetCategory="kr_stock" if mkt == "kr" else "us_stock",
                    displayName=sym,
                    quantity=1,
                    averageCost=100,
                    costBasis=100,
                    currency="KRW" if mkt == "kr" else "USD",
                    valueNative=110,
                    valueKrw=110,
                    pnlKrw=10,
                    pnlRate=0.1,
                )
            )

    app = FastAPI()
    app.include_router(invest_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService(
        stub_holdings
    )

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


async def _seed(
    db_session,
    dedup_key: str,
    *,
    source: str = "naver_research",
    symbol: str = "AAPL",
    market: str = "us",
    category: str | None = None,
    analyst: str | None = None,
    published_at: datetime | None | object = _UNSET,
    detail_excerpt: str | None = "excerpt body",
):
    from app.models.research_reports import ResearchReport

    effective_published_at = (
        datetime.now(UTC) if published_at is _UNSET else published_at
    )
    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
        title=f"Title {dedup_key}",
        analyst=analyst,
        category=category,
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt=detail_excerpt,
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": market, "source": "t"}],
        attribution_publisher="test_publisher",
        attribution_copyright_notice="© Test",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=effective_published_at,
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.integration
def test_auth_required():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.invest_api import router as invest_router

    app = FastAPI()
    app.include_router(invest_router)
    with TestClient(app) as c:
        r = c.get("/invest/api/feed/research")
        assert r.status_code in (401, 403, 422)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_result_for_unknown_source(db_session):
    source = f"test_empty_src_{uuid4()}"
    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["nextCursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_symbol_filter_narrows_results(db_session):
    source = f"test_sym_router_{uuid4()}"
    await _seed(
        db_session, f"sr-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"sr-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&symbol=AAPL")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["symbolCandidates"][0]["symbol"] == "AAPL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_filter(db_session):
    src_a = f"src_a_router_{uuid4()}"
    src_b = f"src_b_router_{uuid4()}"
    await _seed(db_session, f"sf-a-{uuid4()}", source=src_a)
    await _seed(db_session, f"sf-b-{uuid4()}", source=src_b)

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={src_a}")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_category_filter(db_session):
    source = f"test_cat_router_{uuid4()}"
    await _seed(db_session, f"cr-a-{uuid4()}", source=source, category="기업분석")
    await _seed(db_session, f"cr-b-{uuid4()}", source=source, category="산업분석")

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&category=기업분석")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyst_filter(db_session):
    source = f"test_analyst_router_{uuid4()}"
    await _seed(db_session, f"ar-a-{uuid4()}", source=source, analyst="Kim Analyst")
    await _seed(db_session, f"ar-b-{uuid4()}", source=source, analyst="Park Researcher")

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&analyst=kim")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_from_date_to_date_filter(db_session):
    source = f"test_date_router_{uuid4()}"
    t0 = datetime(2026, 5, 1, tzinfo=UTC)
    await _seed(
        db_session,
        f"dr-old-{uuid4()}",
        source=source,
        published_at=t0 - timedelta(days=10),
    )
    await _seed(db_session, f"dr-in-{uuid4()}", source=source, published_at=t0)
    await _seed(
        db_session,
        f"dr-new-{uuid4()}",
        source=source,
        published_at=t0 + timedelta(days=10),
    )

    with TestClient(_make_app()) as c:
        r = c.get(
            f"/invest/api/feed/research?source={source}&fromDate=2026-04-25&toDate=2026-05-05"
        )
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1


@pytest.mark.integration
def test_from_date_greater_than_to_date_returns_400():
    with TestClient(_make_app()) as c:
        r = c.get("/invest/api/feed/research?fromDate=2026-05-10&toDate=2026-05-01")
        assert r.status_code == 400


@pytest.mark.integration
def test_limit_0_returns_422():
    with TestClient(_make_app()) as c:
        r = c.get("/invest/api/feed/research?limit=0")
        assert r.status_code == 422


@pytest.mark.integration
def test_limit_101_returns_422():
    with TestClient(_make_app()) as c:
        r = c.get("/invest/api/feed/research?limit=101")
        assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_100_honored(db_session):
    source = f"test_limit_100_{uuid4()}"
    for i in range(5):
        await _seed(db_session, f"l100-{i}-{uuid4()}", source=source)

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&limit=100")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_round_trip_pagination(db_session):
    source = f"test_cursor_router_{uuid4()}"
    base_dt = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed(
            db_session,
            f"cr-{i}-{uuid4()}",
            source=source,
            published_at=base_dt - timedelta(hours=i),
        )

    with TestClient(_make_app()) as c:
        r1 = c.get(f"/invest/api/feed/research?source={source}&limit=2")
        assert r1.status_code == 200
        b1 = r1.json()
        assert len(b1["items"]) == 2
        assert b1["nextCursor"] is not None

        r2 = c.get(
            f"/invest/api/feed/research?source={source}&limit=2&cursor={b1['nextCursor']}"
        )
        assert r2.status_code == 200
        b2 = r2.json()
        assert len(b2["items"]) == 2

        ids1 = {item["id"] for item in b1["items"]}
        ids2 = {item["id"] for item in b2["items"]}
        assert ids1.isdisjoint(ids2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_last_page_returns_null_next_cursor(db_session):
    source = f"test_last_page_router_{uuid4()}"
    for i in range(3):
        await _seed(db_session, f"lp-{i}-{uuid4()}", source=source)

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&limit=10")
        assert r.status_code == 200
        assert r.json()["nextCursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tab_mine_filters_to_holdings(db_session):
    source = f"test_mine_router_{uuid4()}"
    await _seed(
        db_session, f"tm-a-{uuid4()}", source=source, symbol="AAPL", market="us"
    )
    await _seed(
        db_session, f"tm-m-{uuid4()}", source=source, symbol="MSFT", market="us"
    )

    with TestClient(_make_app(held=[("us", "AAPL")])) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&tab=mine")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["symbolCandidates"][0]["symbol"] == "AAPL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tab_kr_filters_by_market(db_session):
    source = f"test_kr_router_{uuid4()}"
    await _seed(
        db_session, f"kr-a-{uuid4()}", source=source, symbol="005930", market="kr"
    )
    await _seed(
        db_session, f"kr-b-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&tab=kr")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["symbolCandidates"][0]["market"] == "kr"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tab_us_filters_by_market(db_session):
    source = f"test_us_router_{uuid4()}"
    await _seed(
        db_session, f"us-a-{uuid4()}", source=source, symbol="005930", market="kr"
    )
    await _seed(
        db_session, f"us-b-{uuid4()}", source=source, symbol="AAPL", market="us"
    )

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}&tab=us")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["symbolCandidates"][0]["market"] == "us"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_field_allowlist(db_session):
    source = f"test_allowlist_{uuid4()}"
    await _seed(db_session, f"al-a-{uuid4()}", source=source)

    with TestClient(_make_app()) as c:
        r = c.get(f"/invest/api/feed/research?source={source}")
        assert r.status_code == 200
        body = r.json()

        # Top-level keys
        assert set(body.keys()) == {"tab", "asOf", "items", "nextCursor", "meta"}

        # Item keys
        item = body["items"][0]
        expected_item_keys = {
            "id",
            "source",
            "title",
            "analyst",
            "publishedAtText",
            "publishedAt",
            "category",
            "detailUrl",
            "pdfUrl",
            "excerpt",
            "symbolCandidates",
            "attributionPublisher",
            "attributionCopyrightNotice",
            "market",
            "relation",
        }
        assert set(item.keys()) == expected_item_keys
