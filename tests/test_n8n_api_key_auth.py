"""Tests for /api/n8n/* API key authentication in AuthMiddleware."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.auth import AuthMiddleware


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app with AuthMiddleware and a dummy n8n route."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/n8n/daily-brief")
    async def daily_brief():
        return {"success": True}

    @app.get("/api/n8n/pending-orders")
    async def pending_orders():
        return {"success": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/openclaw/callback")
    async def openclaw_callback():
        return {"ok": True}

    return app


class TestN8nApiKeyAuth:
    """N8N API key authentication via AuthMiddleware."""

    @pytest.fixture
    def client_with_key(self) -> TestClient:
        """Client where N8N_API_KEY is configured."""
        with patch("app.middleware.auth.settings") as mock_settings:
            mock_settings.N8N_API_KEY = "test-secret-key-123"
            mock_settings.DOCS_ENABLED = False
            mock_settings.PUBLIC_API_PATHS = []
            app = _build_app()
            yield TestClient(app)

    @pytest.fixture
    def client_without_key(self) -> TestClient:
        """Client where N8N_API_KEY is empty (not configured)."""
        with patch("app.middleware.auth.settings") as mock_settings:
            mock_settings.N8N_API_KEY = ""
            mock_settings.DOCS_ENABLED = False
            mock_settings.PUBLIC_API_PATHS = []
            app = _build_app()
            yield TestClient(app)

    def test_no_api_key_returns_401(self, client_with_key: TestClient) -> None:
        """Request without X-N8N-API-KEY header → 401."""
        response = client_with_key.get("/api/n8n/daily-brief")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid N8N API key"

    def test_wrong_api_key_returns_401(self, client_with_key: TestClient) -> None:
        """Request with wrong API key → 401."""
        response = client_with_key.get(
            "/api/n8n/daily-brief",
            headers={"X-N8N-API-KEY": "wrong-key"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid N8N API key"

    def test_correct_api_key_returns_200(self, client_with_key: TestClient) -> None:
        """Request with correct API key → 200."""
        response = client_with_key.get(
            "/api/n8n/daily-brief",
            headers={"X-N8N-API-KEY": "test-secret-key-123"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_correct_key_works_for_all_n8n_paths(
        self, client_with_key: TestClient
    ) -> None:
        """All /api/n8n/* paths accept valid key."""
        response = client_with_key.get(
            "/api/n8n/pending-orders",
            headers={"X-N8N-API-KEY": "test-secret-key-123"},
        )
        assert response.status_code == 200

    def test_unconfigured_key_returns_403(
        self, client_without_key: TestClient
    ) -> None:
        """When N8N_API_KEY is empty → 403 regardless of header."""
        response = client_without_key.get(
            "/api/n8n/daily-brief",
            headers={"X-N8N-API-KEY": "anything"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "N8N_API_KEY not configured"

    def test_unconfigured_key_no_header_returns_403(
        self, client_without_key: TestClient
    ) -> None:
        """When N8N_API_KEY is empty and no header → 403."""
        response = client_without_key.get("/api/n8n/daily-brief")
        assert response.status_code == 403

    def test_health_endpoint_unaffected(self, client_with_key: TestClient) -> None:
        """Health endpoint still works without n8n key."""
        response = client_with_key.get("/health")
        assert response.status_code == 200

    def test_other_public_api_unaffected(self, client_with_key: TestClient) -> None:
        """Other public API paths are not affected by n8n auth."""
        response = client_with_key.get("/api/v1/openclaw/callback")
        assert response.status_code == 200

