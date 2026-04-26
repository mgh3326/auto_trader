from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolio
from app.schemas.order_intent_preview import (
    OrderIntentPreviewRequest,
    OrderIntentPreviewResponse,
)
from app.services.portfolio_decision_service import PortfolioDecisionRunNotFoundError


def _make_client():
    app = FastAPI()
    app.include_router(portfolio.router)
    fake_preview = AsyncMock()
    fake_preview.build_preview = AsyncMock(
        return_value=OrderIntentPreviewResponse(
            decision_run_id="decision-stored", intents=[]
        )
    )
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_order_intent_preview_service] = lambda: (
        fake_preview
    )
    return TestClient(app), fake_preview


@pytest.mark.unit
def test_preview_endpoint_returns_preview_only_response() -> None:
    client, fake_preview = _make_client()
    response = client.post(
        "/portfolio/api/decision-runs/decision-stored/intent-preview",
        json={
            "budget": {},
            "selections": [],
            "execution_mode": "requires_final_approval",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "preview_only"
    assert body["decision_run_id"] == "decision-stored"
    fake_preview.build_preview.assert_awaited_once()
    kwargs = fake_preview.build_preview.await_args.kwargs
    assert kwargs["user_id"] == 7
    assert kwargs["run_id"] == "decision-stored"
    assert isinstance(kwargs["request"], OrderIntentPreviewRequest)
    assert kwargs["request"].execution_mode == "requires_final_approval"


@pytest.mark.unit
def test_preview_endpoint_returns_404_when_run_missing() -> None:
    client, fake_preview = _make_client()
    fake_preview.build_preview.side_effect = PortfolioDecisionRunNotFoundError("nope")
    response = client.post(
        "/portfolio/api/decision-runs/nope/intent-preview",
        json={},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Decision run not found."}


from app.services.order_intent_preview_service import OrderIntentPreviewService


def _make_client_with_real_preview_service():
    app = FastAPI()
    app.include_router(portfolio.router)

    fake_decision_service = AsyncMock()
    fake_decision_service.get_decision_run = AsyncMock(
        return_value={
            "success": True,
            "decision_run": {
                "id": "decision-r1",
                "generated_at": "2026-04-20T10:00:00+00:00",
                "mode": "analysis_only",
                "persisted": True,
                "source": "portfolio_decision_service_v1",
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

    real_preview_service = OrderIntentPreviewService(
        decision_service=fake_decision_service
    )

    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_order_intent_preview_service] = (
        lambda: real_preview_service
    )
    return TestClient(app)


@pytest.mark.unit
def test_preview_endpoint_response_includes_discord_brief_with_run_path() -> None:
    client = _make_client_with_real_preview_service()

    response = client.post(
        "/portfolio/api/decision-runs/decision-r1/intent-preview",
        json={
            "budget": {"default_buy_budget_krw": 100000},
            "selections": [],
            "execution_mode": "requires_final_approval",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "preview_only"
    assert "discord_brief" in body
    assert body["discord_brief"] is not None
    # Path/query substring is asserted (not full origin) — TestClient base
    # URL is environment-dependent.
    assert "/portfolio/decision?run_id=decision-r1" in body["discord_brief"]
    assert "Mode: `preview_only`" in body["discord_brief"]
    assert "This is preview-only." in body["discord_brief"]
