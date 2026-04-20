from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolio


class _FakeDecisionService:
    def __init__(self) -> None:
        self.build_decision_slate = AsyncMock(
            return_value={
                "success": True,
                "decision_run": {
                    "id": "test-run",
                    "generated_at": "2026-04-20T10:00:00",
                    "mode": "analysis_only",
                    "persisted": False,
                    "source": "test",
                },
                "filters": {"market": "ALL", "account_keys": [], "q": None},
                "summary": {
                    "symbols": 0,
                    "decision_items": 0,
                    "actionable_items": 0,
                    "manual_review_items": 0,
                    "auto_candidate_items": 0,
                    "missing_context_items": 0,
                    "by_action": {},
                    "by_market": {},
                },
                "facets": {"accounts": []},
                "symbol_groups": [],
                "warnings": [],
            }
        )


def _create_client() -> tuple[TestClient, _FakeDecisionService]:
    app = FastAPI()
    fake_service = _FakeDecisionService()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_portfolio_decision_service] = lambda: (
        fake_service
    )
    return TestClient(app), fake_service


@pytest.mark.unit
def test_portfolio_decision_page_renders_html() -> None:
    client, _ = _create_client()
    response = client.get("/portfolio/decision")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert 'id="portfolio-decision-desk-page"' in response.text


@pytest.mark.unit
def test_get_portfolio_decision_slate_api() -> None:
    client, fake_service = _create_client()
    response = client.get("/portfolio/api/decision-slate")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["decision_run"]["id"] == "test-run"

    fake_service.build_decision_slate.assert_awaited_once()


@pytest.mark.unit
def test_get_portfolio_decision_slate_uses_safe_error_detail() -> None:
    client, fake_service = _create_client()
    fake_service.build_decision_slate.side_effect = RuntimeError(
        "upstream token secret leaked"
    )

    response = client.get("/portfolio/api/decision-slate")

    assert response.status_code == 500
    assert response.json() == {"detail": "Unable to build portfolio decision slate."}
    assert "secret" not in response.text
