from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers import screener


class _FakeScreenerService:
    def __init__(self) -> None:
        self.process_callback = AsyncMock(
            return_value={"status": "ok", "request_id": "job-1", "job_id": "job-1"}
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "OPENCLAW_CALLBACK_TOKEN", "callback-secret")

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(screener.router)
    fake_service = _FakeScreenerService()
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app) as test_client:
        yield test_client


def _payload() -> dict:
    return {
        "request_id": "job-1",
        "symbol": "AAPL",
        "name": "Apple",
        "instrument_type": "equity_us",
        "decision": "hold",
        "confidence": 55,
        "reasons": ["r1"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        "detailed_text": "report",
    }


def test_screener_callback_rejects_missing_token(client: TestClient) -> None:
    response = client.post("/api/screener/callback", json=_payload())
    assert response.status_code == 401


def test_screener_callback_rejects_invalid_token(client: TestClient) -> None:
    response = client.post(
        "/api/screener/callback",
        headers={"Authorization": "Bearer wrong"},
        json=_payload(),
    )
    assert response.status_code == 401


def test_screener_callback_accepts_valid_bearer_token(client: TestClient) -> None:
    response = client.post(
        "/api/screener/callback",
        headers={"Authorization": "Bearer callback-secret"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_screener_callback_accepts_x_openclaw_token(client: TestClient) -> None:
    response = client.post(
        "/api/screener/callback",
        headers={"X-OpenClaw-Token": "callback-secret"},
        json=_payload(),
    )
    assert response.status_code == 200
