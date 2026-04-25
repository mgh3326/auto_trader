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
    app.dependency_overrides[portfolio.get_order_intent_preview_service] = (
        lambda: fake_preview
    )
    return TestClient(app), fake_preview


@pytest.mark.unit
def test_preview_endpoint_returns_preview_only_response() -> None:
    client, fake_preview = _make_client()
    response = client.post(
        "/portfolio/api/decision-runs/decision-stored/intent-preview",
        json={"budget": {}, "selections": [], "execution_mode": "requires_final_approval"},
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
