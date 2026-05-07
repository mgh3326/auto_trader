"""Read-only research reports router (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    from app.models.research_reports import (
        ResearchReport,
        ResearchReportIngestionRun,
    )

    await db_session.execute(delete(ResearchReport))
    await db_session.execute(delete(ResearchReportIngestionRun))
    await db_session.commit()
    yield


def _app() -> FastAPI:
    from app.core.db import get_db
    from app.routers import research_reports as router_module
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=42)

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


async def _seed(db_session, dedup_key="r-1", *, symbol="AAPL"):
    from app.models.research_reports import ResearchReport

    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source="naver_research",
        title=f"Title {dedup_key}",
        summary_text="summary",
        detail_url=f"https://example.com/{dedup_key}",
        detail_excerpt="excerpt",
        pdf_url=f"https://example.com/{dedup_key}.pdf",
        symbol_candidates=[{"symbol": symbol, "market": "us", "source": "t"}],
        attribution_publisher="naver_research",
        attribution_copyright_notice="© Naver",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()


@pytest.mark.integration
def test_recent_endpoint_returns_empty(db_session):
    with TestClient(_app()) as client:
        resp = client.get("/trading/api/research-reports/recent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["citations"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_endpoint_filters_by_symbol(db_session):
    await _seed(db_session, "x-1", symbol="AAPL")
    await _seed(db_session, "x-2", symbol="MSFT")
    with TestClient(_app()) as client:
        resp = client.get("/trading/api/research-reports/recent?symbol=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["citations"][0]["title"] == "Title x-1"


@pytest.mark.integration
def test_recent_endpoint_unauthorized_without_override():
    from app.routers import research_reports as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    with TestClient(app) as client:
        resp = client.get("/trading/api/research-reports/recent")
        assert resp.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_response_does_not_include_body_fields(db_session):
    await _seed(db_session, "y-1", symbol="AAPL")
    with TestClient(_app()) as client:
        resp = client.get("/trading/api/research-reports/recent?symbol=AAPL")
        body = resp.json()
        assert body["count"] == 1
        citation = body["citations"][0]
        for forbidden in (
            "pdf_body",
            "pdf_text",
            "full_text",
            "article_content",
            "article_body",
            "raw_payload",
        ):
            assert forbidden not in citation
