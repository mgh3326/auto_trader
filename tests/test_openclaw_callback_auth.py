"""Authentication tests for the OpenClaw callback endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers import openclaw_callback


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "OPENCLAW_CALLBACK_TOKEN", "callback-secret")

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(openclaw_callback.router)

    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def refresh_side_effect(instance):
        instance.id = 1

    db.refresh = AsyncMock(side_effect=refresh_side_effect)

    async def override_get_db():
        yield db

    app.dependency_overrides[openclaw_callback.get_db] = override_get_db

    mock_create_stock = AsyncMock()
    mock_create_stock.return_value.id = 42

    with patch(
        "app.routers.openclaw_callback.create_stock_if_not_exists",
        new=mock_create_stock,
    ):
        with TestClient(app) as c:
            yield c


def _payload() -> dict:
    return {
        "request_id": "r1",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "instrument_type": "equity_us",
        "decision": "hold",
        "confidence": 58,
        "reasons": ["a"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        "detailed_text": "md",
        "model_name": "openai/gpt-x",
    }


def test_openclaw_callback_rejects_missing_token(client: TestClient):
    res = client.post("/api/v1/openclaw/callback", json=_payload())
    assert res.status_code == 401


def test_openclaw_callback_rejects_invalid_token(client: TestClient):
    res = client.post(
        "/api/v1/openclaw/callback",
        headers={"Authorization": "Bearer wrong"},
        json=_payload(),
    )
    assert res.status_code == 401


def test_openclaw_callback_accepts_valid_bearer_token(client: TestClient):
    res = client.post(
        "/api/v1/openclaw/callback",
        headers={"Authorization": "Bearer callback-secret"},
        json=_payload(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "r1"


def test_openclaw_callback_accepts_x_openclaw_token(client: TestClient):
    res = client.post(
        "/api/v1/openclaw/callback",
        headers={"X-OpenClaw-Token": "callback-secret"},
        json=_payload(),
    )
    assert res.status_code == 200
