"""Read-only research reports router (ROB-140)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


async def _seed(db_session, dedup_key="r-1", *, source="naver_research", symbol="AAPL"):
    from app.models.research_reports import ResearchReport

    row = ResearchReport(
        dedup_key=dedup_key,
        report_type="equity_research",
        source=source,
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
    source = f"test_empty_source_{uuid4()}"
    with TestClient(_app()) as client:
        resp = client.get(f"/trading/api/research-reports/recent?source={source}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["citations"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_endpoint_filters_by_symbol(db_session):
    source = f"test_router_symbol_filter_{uuid4()}"
    aapl_key = f"x-1-{uuid4()}"
    await _seed(db_session, aapl_key, source=source, symbol="AAPL")
    await _seed(db_session, f"x-2-{uuid4()}", source=source, symbol="MSFT")
    with TestClient(_app()) as client:
        resp = client.get(
            f"/trading/api/research-reports/recent?symbol=AAPL&source={source}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["citations"][0]["title"] == f"Title {aapl_key}"


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
    source = f"test_router_no_body_fields_{uuid4()}"
    await _seed(db_session, f"y-1-{uuid4()}", source=source, symbol="AAPL")
    with TestClient(_app()) as client:
        resp = client.get(
            f"/trading/api/research-reports/recent?symbol=AAPL&source={source}"
        )
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
