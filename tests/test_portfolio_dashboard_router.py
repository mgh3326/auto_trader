from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolio


class _FakeOverviewService:
    def __init__(self) -> None:
        self.get_overview = AsyncMock(
            return_value={
                "success": True,
                "as_of": "2026-02-20T00:00:00+00:00",
                "filters": {
                    "market": "US",
                    "account_keys": ["live:kis", "manual:1"],
                    "q": "aapl",
                },
                "summary": {
                    "total_positions": 1,
                    "by_market": {"KR": 0, "US": 1, "CRYPTO": 0},
                },
                "facets": {
                    "accounts": [
                        {
                            "account_key": "live:kis",
                            "broker": "kis",
                            "account_name": "KIS 실계좌",
                            "source": "live",
                            "market_types": ["US"],
                        }
                    ]
                },
                "positions": [
                    {
                        "market_type": "US",
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "quantity": 2,
                        "avg_price": 150.0,
                        "current_price": 160.0,
                        "evaluation": 320.0,
                        "profit_loss": 20.0,
                        "profit_rate": 0.0667,
                        "components": [],
                    }
                ],
                "warnings": [],
            }
        )


def _create_client() -> tuple[TestClient, _FakeOverviewService]:
    app = FastAPI()
    fake_service = _FakeOverviewService()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_portfolio_overview_service] = lambda: (
        fake_service
    )
    return TestClient(app), fake_service


def test_portfolio_dashboard_page_renders_screener_style_shell() -> None:
    client, _ = _create_client()
    response = client.get("/portfolio/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="portfolio-main-page"' in body
    assert 'id="portfolio-table"' in body
    assert 'id="portfolio-cards"' in body
    assert "@media (max-width: 760px)" in body
    assert "function escapeHtml(value)" in body
    assert "escapeHtml(item.name)" in body
    assert "escapeHtml(warning)" in body


def test_portfolio_overview_api_passes_repeated_account_keys() -> None:
    client, fake_service = _create_client()
    response = client.get(
        "/portfolio/api/overview",
        params=[
            ("market", "US"),
            ("account_keys", "live:kis"),
            ("account_keys", "manual:1"),
            ("q", "aapl"),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["filters"]["market"] == "US"
    assert payload["filters"]["account_keys"] == ["live:kis", "manual:1"]

    fake_service.get_overview.assert_awaited_once_with(
        user_id=7,
        market="US",
        account_keys=["live:kis", "manual:1"],
        q="aapl",
    )


def test_portfolio_overview_api_uses_default_filters() -> None:
    client, fake_service = _create_client()
    response = client.get("/portfolio/api/overview")

    assert response.status_code == 200
    fake_service.get_overview.assert_awaited_once_with(
        user_id=7,
        market="ALL",
        account_keys=None,
        q=None,
    )


def test_portfolio_overview_api_rejects_invalid_market() -> None:
    client, fake_service = _create_client()
    response = client.get("/portfolio/api/overview", params={"market": "INVALID"})

    assert response.status_code == 422
    fake_service.get_overview.assert_not_awaited()
