"""Copyright guardrails for /invest/api/feed/research (ROB-179).

Defense-in-depth: asserts that the response JSON never contains body/full-text fields.
The model has no body columns (structural guarantee), these tests are the assertion layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_FORBIDDEN_RESPONSE_FIELDS = frozenset(
    {
        "pdf_body",
        "pdf_text",
        "extracted_text",
        "full_text",
        "article_content",
        "article_body",
        "raw_payload",
        "raw_payload_json",
        "dedup_key",
        "source_report_id",
        "ingestion_run_id",
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "pdf_filename",
        "pdf_text_length",
        "attribution_full_text_exported",
        "attribution_pdf_body_exported",
        "raw_text_policy",
    }
)


def _make_app():
    from app.core.db import get_db
    from app.routers.dependencies import get_authenticated_user
    from app.routers.invest_api import get_invest_home_service
    from app.routers.invest_api import router as invest_router
    from app.schemas.invest_home import (
        InvestHomeResponse,
        InvestHomeResponseMeta,
    )
    from app.services.invest_home_service import (
        build_grouped_holdings,
        build_home_summary,
    )

    class _StubService:
        async def get_home(self, *, user_id: int) -> InvestHomeResponse:
            return InvestHomeResponse(
                homeSummary=build_home_summary([]),
                accounts=[],
                holdings=[],
                groupedHoldings=build_grouped_holdings([]),
                meta=InvestHomeResponseMeta(warnings=[]),
            )

    app = FastAPI()
    app.include_router(invest_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _find_forbidden_keys(obj, forbidden: frozenset[str]) -> list[str]:
    """Recursively scan a JSON object for any key in forbidden."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in forbidden:
                found.append(k)
            found.extend(_find_forbidden_keys(v, forbidden))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_forbidden_keys(item, forbidden))
    return found


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_excludes_body_fields(db_session):
    from app.models.research_reports import ResearchReport

    source = f"test_guardrail_no_body_{uuid4()}"
    row = ResearchReport(
        dedup_key=f"grb-{uuid4()}",
        report_type="equity_research",
        source=source,
        title="Guardrail test",
        summary_text="summary",
        detail_excerpt="excerpt",
        symbol_candidates=[{"symbol": "AAPL", "market": "us", "source": "t"}],
        attribution_publisher="test",
        attribution_copyright_notice="© Test",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()

    with TestClient(_make_app()) as c:
        resp = c.get(f"/invest/api/feed/research?source={source}")
        assert resp.status_code == 200
        body = resp.json()
        forbidden_found = _find_forbidden_keys(body, _FORBIDDEN_RESPONSE_FIELDS)
        assert not forbidden_found, (
            f"Response contains forbidden body fields: {forbidden_found}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_excerpt_capped_at_500_chars(db_session):
    from app.models.research_reports import ResearchReport

    source = f"test_guardrail_excerpt_{uuid4()}"
    long_excerpt = "X" * 600
    row = ResearchReport(
        dedup_key=f"gre-{uuid4()}",
        report_type="equity_research",
        source=source,
        title="Excerpt cap test",
        summary_text="summary",
        detail_excerpt=long_excerpt,
        symbol_candidates=[{"symbol": "AAPL", "market": "us", "source": "t"}],
        attribution_publisher="test",
        attribution_copyright_notice="© Test",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()

    with TestClient(_make_app()) as c:
        resp = c.get(f"/invest/api/feed/research?source={source}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        excerpt = body["items"][0]["excerpt"]
        assert excerpt is not None
        assert len(excerpt) <= 500, f"Excerpt length {len(excerpt)} exceeds 500 chars"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attribution_publisher_exposed_when_present(db_session):
    from app.models.research_reports import ResearchReport

    source = f"test_guardrail_pub_{uuid4()}"
    row = ResearchReport(
        dedup_key=f"grp-{uuid4()}",
        report_type="equity_research",
        source=source,
        title="Attribution test",
        summary_text="summary",
        symbol_candidates=[{"symbol": "AAPL", "market": "us", "source": "t"}],
        attribution_publisher="Korea Investment & Securities",
        attribution_copyright_notice="© Korea Investment",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()

    with TestClient(_make_app()) as c:
        resp = c.get(f"/invest/api/feed/research?source={source}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["attributionPublisher"] == "Korea Investment & Securities"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attribution_copyright_notice_exposed_when_present(db_session):
    from app.models.research_reports import ResearchReport

    source = f"test_guardrail_copy_{uuid4()}"
    row = ResearchReport(
        dedup_key=f"grc-{uuid4()}",
        report_type="equity_research",
        source=source,
        title="Copyright test",
        summary_text="summary",
        symbol_candidates=[{"symbol": "AAPL", "market": "us", "source": "t"}],
        attribution_publisher="Korea Investment",
        attribution_copyright_notice="© Korea Investment & Securities / Truefriend",
        attribution_full_text_exported=False,
        attribution_pdf_body_exported=False,
        published_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()

    with TestClient(_make_app()) as c:
        resp = c.get(f"/invest/api/feed/research?source={source}")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert (
            item["attributionCopyrightNotice"]
            == "© Korea Investment & Securities / Truefriend"
        )
