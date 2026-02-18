from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.routers import screener
from app.services.screener_service import ScreenerService


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return False
        _ = ex
        self.store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str):
        _ = ttl
        self.store[key] = value
        return True

    async def delete(self, key: str):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def screener_app_success(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, AsyncMock]]:
    monkeypatch.setattr(settings, "OPENCLAW_CALLBACK_TOKEN", "callback-secret")

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(return_value="job-e2e")
    service = ScreenerService(redis_client=fake_redis, openclaw_client=openclaw)

    app = FastAPI()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: service

    with TestClient(app) as client:
        yield client, openclaw


@pytest.fixture
def screener_app_openclaw_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient]:
    monkeypatch.setattr(settings, "OPENCLAW_CALLBACK_TOKEN", "callback-secret")

    fake_redis = _FakeRedis()
    openclaw = AsyncMock()
    openclaw.request_analysis = AsyncMock(side_effect=RuntimeError("openclaw down"))
    service = ScreenerService(redis_client=fake_redis, openclaw_client=openclaw)

    app = FastAPI()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: service

    with TestClient(app) as client:
        yield client


@pytest.mark.integration
def test_screener_report_lifecycle_e2e(
    screener_app_success: tuple[TestClient, AsyncMock],
) -> None:
    client, openclaw = screener_app_success

    create_res = client.post(
        "/api/screener/report",
        json={"market": "us", "symbol": "AAPL", "name": "Apple"},
    )
    assert create_res.status_code == 200
    create_body = create_res.json()
    assert create_body["job_id"] == "job-e2e"
    assert create_body["status"] in {"queued", "running"}
    openclaw.request_analysis.assert_awaited_once()

    running_res = client.get("/api/screener/report/job-e2e")
    assert running_res.status_code == 200
    running_body = running_res.json()
    assert running_body["job_id"] == "job-e2e"
    assert running_body["status"] in {"queued", "running"}

    callback_res = client.post(
        "/api/screener/callback",
        headers={"Authorization": "Bearer callback-secret"},
        json={
            "request_id": "job-e2e",
            "symbol": "AAPL",
            "name": "Apple",
            "instrument_type": "equity_us",
            "decision": "hold",
            "confidence": 62,
            "reasons": ["range"],
            "price_analysis": {
                "appropriate_buy_range": {"min": 100, "max": 110},
                "appropriate_sell_range": {"min": 120, "max": 130},
                "buy_hope_range": {"min": 95, "max": 99},
                "sell_target_range": {"min": 140, "max": 150},
            },
            "detailed_text": "stable trend",
        },
    )
    assert callback_res.status_code == 200
    assert callback_res.json()["status"] == "ok"

    completed_res = client.get("/api/screener/report/job-e2e")
    assert completed_res.status_code == 200
    completed_body = completed_res.json()
    assert completed_body["status"] == "completed"
    assert completed_body["report"]["decision"] == "hold"

    unknown_res = client.get("/api/screener/report/unknown-job-id")
    assert unknown_res.status_code == 200
    assert unknown_res.json() == {
        "job_id": "unknown-job-id",
        "status": "failed",
        "error": "job_not_found",
        "not_found": True,
    }


@pytest.mark.integration
def test_screener_report_failure_contains_error(
    screener_app_openclaw_failure: TestClient,
) -> None:
    client = screener_app_openclaw_failure

    create_res = client.post(
        "/api/screener/report",
        json={"market": "us", "symbol": "AAPL", "name": "Apple"},
    )
    assert create_res.status_code == 200
    create_body = create_res.json()
    assert create_body["status"] == "failed"
    assert "openclaw down" in create_body["error"]

    status_res = client.get(f"/api/screener/report/{create_body['job_id']}")
    assert status_res.status_code == 200
    status_body = status_res.json()
    assert status_body["status"] == "failed"
    assert "openclaw down" in status_body["error"]
