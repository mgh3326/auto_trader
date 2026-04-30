"""Tests for news-ingestor bulk ingest machine-to-machine auth."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.auth import AuthMiddleware

_HEADER_NAME = "X-News-Ingestor-Token"
_EXPECTED_VALUE = "expected-ingest-value-123"
_WRONG_VALUE = "wrong-ingest-value-456"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.post("/api/v1/news/ingest/bulk")
    async def news_ingest_bulk():
        return {"success": True}

    @app.get("/api/v1/news/readiness")
    async def news_readiness():
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


@pytest.fixture
def client_with_ingest_value() -> TestClient:
    with patch("app.middleware.auth.settings") as mock_settings:
        mock_settings.DOCS_ENABLED = False
        mock_settings.PUBLIC_API_PATHS = []
        mock_settings.N8N_API_KEY = ""
        mock_settings.NEWS_INGESTOR_INGEST_TOKEN = _EXPECTED_VALUE
        mock_settings.NEWS_INGESTOR_INGEST_TOKEN_HEADER = _HEADER_NAME
        yield TestClient(_build_app())


@pytest.fixture
def client_without_ingest_value() -> TestClient:
    with patch("app.middleware.auth.settings") as mock_settings:
        mock_settings.DOCS_ENABLED = False
        mock_settings.PUBLIC_API_PATHS = []
        mock_settings.N8N_API_KEY = ""
        mock_settings.NEWS_INGESTOR_INGEST_TOKEN = ""
        mock_settings.NEWS_INGESTOR_INGEST_TOKEN_HEADER = _HEADER_NAME
        yield TestClient(_build_app())


def test_bulk_ingest_fails_closed_when_env_value_unset(
    client_without_ingest_value: TestClient,
) -> None:
    response = client_without_ingest_value.post(
        "/api/v1/news/ingest/bulk",
        headers={_HEADER_NAME: _EXPECTED_VALUE},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "News ingestor ingest token not configured"
    assert _EXPECTED_VALUE not in response.text


def test_bulk_ingest_missing_header_returns_401(
    client_with_ingest_value: TestClient,
) -> None:
    response = client_with_ingest_value.post("/api/v1/news/ingest/bulk")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid news ingestor ingest token"
    assert _EXPECTED_VALUE not in response.text


def test_bulk_ingest_wrong_header_value_returns_401(
    client_with_ingest_value: TestClient,
) -> None:
    response = client_with_ingest_value.post(
        "/api/v1/news/ingest/bulk",
        headers={_HEADER_NAME: _WRONG_VALUE},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid news ingestor ingest token"
    assert _WRONG_VALUE not in response.text
    assert _EXPECTED_VALUE not in response.text


def test_bulk_ingest_correct_header_value_reaches_route(
    client_with_ingest_value: TestClient,
) -> None:
    response = client_with_ingest_value.post(
        "/api/v1/news/ingest/bulk",
        headers={_HEADER_NAME: _EXPECTED_VALUE},
    )

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert _EXPECTED_VALUE not in response.text


def test_other_news_api_still_uses_session_auth(
    client_with_ingest_value: TestClient,
) -> None:
    response = client_with_ingest_value.get(
        "/api/v1/news/readiness",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required for this endpoint."


def test_health_endpoint_unaffected(client_with_ingest_value: TestClient) -> None:
    response = client_with_ingest_value.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
