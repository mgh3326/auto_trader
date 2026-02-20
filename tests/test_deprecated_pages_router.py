from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import deprecated_pages


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(deprecated_pages.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/manual-holdings/",
        "/kis-domestic-trading/",
        "/kis-overseas-trading/",
        "/upbit-trading/",
    ],
)
def test_legacy_page_prefixes_return_410_html(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Accept": "text/html"})

    assert response.status_code == 410
    assert "text/html" in response.headers.get("content-type", "")
    assert "410 Gone" in response.text
    assert "/portfolio/" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/manual-holdings/api/holdings",
        "/kis-domestic-trading/api/my-stocks",
        "/kis-overseas-trading/api/my-stocks",
        "/upbit-trading/api/my-coins",
    ],
)
def test_legacy_api_prefixes_return_410_json(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Accept": "application/json"})

    assert response.status_code == 410
    assert "application/json" in response.headers.get("content-type", "")

    payload = response.json()
    assert "detail" in payload
    assert payload["replacement_url"] == "/portfolio/"
    assert payload["deprecated_at"]


def test_legacy_api_post_also_returns_410_json(client: TestClient) -> None:
    response = client.post(
        "/upbit-trading/api/buy-orders",
        headers={"Accept": "application/json"},
        json={"any": "payload"},
    )

    assert response.status_code == 410
    payload = response.json()
    assert payload["replacement_url"] == "/portfolio/"
