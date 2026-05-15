"""ROB-207 middleware token-auth tests for research reports bulk ingest."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app_with_middleware(monkeypatch, *, token: str = "secret-test-token"):
    from app.core.config import settings
    from app.middleware.auth import AuthMiddleware
    from app.routers import research_reports as router_module

    monkeypatch.setattr(settings, "RESEARCH_REPORTS_INGEST_TOKEN", token, raising=False)
    monkeypatch.setattr(
        settings,
        "RESEARCH_REPORTS_INGEST_TOKEN_HEADER",
        "X-Research-Reports-Ingest-Token",
        raising=False,
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(router_module.router)
    return app


@pytest.mark.integration
def test_bulk_ingest_requires_token(monkeypatch):
    app = _app_with_middleware(monkeypatch)
    with TestClient(app) as client:
        resp = client.post("/trading/api/research-reports/ingest/bulk", json={})
        assert resp.status_code == 401


@pytest.mark.integration
def test_bulk_ingest_403_when_token_not_configured(monkeypatch):
    app = _app_with_middleware(monkeypatch, token="")
    with TestClient(app) as client:
        resp = client.post(
            "/trading/api/research-reports/ingest/bulk",
            json={},
            headers={"X-Research-Reports-Ingest-Token": "anything"},
        )
        assert resp.status_code == 403


@pytest.mark.integration
def test_bulk_ingest_accepts_valid_token_rejects_bad_payload(monkeypatch):
    app = _app_with_middleware(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/trading/api/research-reports/ingest/bulk",
            json={"reports": []},
            headers={"X-Research-Reports-Ingest-Token": "secret-test-token"},
        )
        # Valid token, invalid payload → 422 (schema validation), not 401/403.
        assert resp.status_code in (400, 422)
