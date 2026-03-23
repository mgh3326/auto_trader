from __future__ import annotations

import re

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
        "/dashboard/",
        "/stock-latest/",
        "/analysis-json/",
        "/orderbook/",
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
        "/dashboard/api/analysis",
        "/stock-latest/api/filters",
        "/analysis-json/api/filters",
        "/orderbook/api/markets",
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


@pytest.mark.parametrize(
    ("path", "expected_prefix"),
    [
        ("/manual-holdings", "/manual-holdings"),
        ("/manual-holdings/api/holdings", "/manual-holdings"),
        ("/upbit-trading/api/buy-orders", "/upbit-trading"),
        ("/dashboard/api/analysis", "/dashboard"),
        ("/stock-latest/api/filters", "/stock-latest"),
        ("/analysis-json/api/filters", "/analysis-json"),
        ("/orderbook/api/markets", "/orderbook"),
    ],
)
def test_legacy_exempt_patterns_match_base_paths_and_subpaths(
    path: str, expected_prefix: str
) -> None:
    matched = [
        pattern.pattern
        for pattern in deprecated_pages.legacy_exempt_url_patterns()
        if pattern.match(path)
    ]

    assert matched
    assert any(re.escape(expected_prefix) in pattern for pattern in matched)
