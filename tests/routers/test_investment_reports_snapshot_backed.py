"""ROB-273 — HTTP entrypoint for snapshot-backed report generation."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.routers.investment_reports import router as reports_router
from app.services.action_report.snapshot_backed.generator import (
    PublishBlockedByStaleGateError,
    SnapshotBackedReportGeneratorError,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationResponse,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(reports_router)

    async def _db_override() -> AsyncIterator[object]:
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        fake_db.rollback = AsyncMock()
        yield fake_db

    async def _user_override() -> User:
        return User(id=1, email="test@example.com", role="user")  # type: ignore[call-arg]

    from app.core.db import get_db

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = _user_override
    return app


_REQUEST_PAYLOAD: dict = {
    "market": "kr",
    "account_scope": "kis_live",
    "status": "published",
    "created_by_profile": "test-runner",
    "title": "Snapshot-backed KR advisory",
    "summary": "테스트 요약",
    "kst_date": "2026-05-19",
    "items": [],
}


@pytest.mark.asyncio
async def test_flag_off_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/trading/api/investment-reports/snapshot-backed",
            json=_REQUEST_PAYLOAD,
        )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "snapshot_backed_report_generator_disabled"


@pytest.mark.asyncio
async def test_flag_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    fake_response = ReportGenerationResponse(
        report_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        snapshot_policy_version="intraday_action_report_v1",
        snapshot_coverage_summary={"required": {"portfolio": "fresh"}},
        snapshot_freshness_summary={"overall": "fresh"},
        source_conflicts={},
        unavailable_sources={},
        items_count=0,
        warnings=[],
        bundle_status="complete",
        bundle_reused=False,
        stale_gate={"reject": False},
    )

    class _FakeGen:
        def __init__(self, *args, **kwargs):
            pass

        async def generate(self, request):
            return fake_response

    app = _build_app()
    with patch(
        "app.routers.investment_reports.SnapshotBackedReportGenerator",
        _FakeGen,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/snapshot-backed",
                json=_REQUEST_PAYLOAD,
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bundle_status"] == "complete"
    assert body["items_count"] == 0


@pytest.mark.asyncio
async def test_flag_on_publish_blocked_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    class _FakeGen:
        def __init__(self, *args, **kwargs):
            pass

        async def generate(self, request):
            raise PublishBlockedByStaleGateError(
                reason="blocked",
                bundle_status="failed",
                freshness_summary={"overall": "failed"},
                stale_gate=None,
            )

    app = _build_app()
    with patch(
        "app.routers.investment_reports.SnapshotBackedReportGenerator",
        _FakeGen,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/snapshot-backed",
                json=_REQUEST_PAYLOAD,
            )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["error"] == "publish_blocked_by_stale_gate"
    assert body["bundle_status"] == "failed"


@pytest.mark.asyncio
async def test_flag_on_bad_request_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )

    class _FakeGen:
        def __init__(self, *args, **kwargs):
            pass

        async def generate(self, request):
            raise SnapshotBackedReportGeneratorError(
                "unsupported market/account_scope pair"
            )

    app = _build_app()
    with patch(
        "app.routers.investment_reports.SnapshotBackedReportGenerator",
        _FakeGen,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/trading/api/investment-reports/snapshot-backed",
                json=_REQUEST_PAYLOAD,
            )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_list_unchanged_for_legacy_reports() -> None:
    """The existing GET surface must not be touched by ROB-273 changes."""
    from app.services.investment_reports.query_service import (
        InvestmentReportQueryService,
    )

    # The list endpoint depends on the query service. We sanity-check that
    # importing the router (with the new POST attached) doesn't break the
    # legacy paths' signature; deeper coverage stays in the existing
    # router tests.
    assert hasattr(InvestmentReportQueryService, "list_reports")
