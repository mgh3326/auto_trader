from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import screener


class _FakeScreenerService:
    def __init__(self) -> None:
        self.list_screening = AsyncMock(
            return_value={
                "results": [{"code": "AAPL"}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
                "cache_hit": False,
            }
        )
        self.refresh_screening = AsyncMock(
            return_value={
                "results": [{"code": "MSFT"}],
                "total_count": 1,
                "returned_count": 1,
                "market": "us",
                "cache_hit": False,
            }
        )
        self.request_report = AsyncMock(
            return_value={"job_id": "job-1", "status": "queued", "is_reused": False}
        )
        self.get_report_status = AsyncMock(
            return_value={"job_id": "job-1", "status": "running"}
        )
        self.process_callback = AsyncMock(
            return_value={"status": "ok", "request_id": "job-1", "job_id": "job-1"}
        )
        self.place_order = AsyncMock(return_value={"success": True, "dry_run": True})


@pytest.fixture
def client() -> Generator[tuple[TestClient, _FakeScreenerService]]:
    app = FastAPI()
    fake_service = _FakeScreenerService()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app) as test_client:
        yield test_client, fake_service


@pytest.fixture
def client_with_user() -> Generator[tuple[TestClient, _FakeScreenerService]]:
    app = FastAPI()
    fake_service = _FakeScreenerService()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        request.state.user = SimpleNamespace(
            username="test-user",
            role=SimpleNamespace(value="user"),
        )
        return await call_next(request)

    _ = _inject_user

    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app) as test_client:
        yield test_client, fake_service


def test_screener_dashboard_page(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, _ = client
    response = test_client.get("/screener")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="screener-main-page"' in body
    assert 'class="page-grid screener-page-grid"' in body
    assert 'class="controls page-primary"' in body
    assert 'class="stack page-sidebar"' in body
    assert 'id="filter-form"' in body
    assert 'id="min-volume"' in body
    assert 'id="limit"' in body
    assert 'max="100"' in body
    assert 'value="50"' in body
    assert 'id="results-table"' in body
    assert 'id="results-cards"' in body
    assert 'class="results-table-wrap screener-results-table-wrap"' in body
    assert "<th>RSI</th>" in body
    assert 'id="report-panel"' in body
    assert 'class="d-flex flex-wrap gap-2 mb-3 report-actions"' in body
    assert 'class="report-table-wrap"' in body
    assert 'id="open-detail-link"' in body
    assert 'id="order-form"' in body
    assert 'id="order-side-tabs"' in body
    assert 'id="order-side"' in body
    assert "pollingEnabled" in body
    assert "nextErrorBackoffDelay" in body


def test_screener_report_page(client: tuple[TestClient, _FakeScreenerService]) -> None:
    test_client, _ = client
    response = test_client.get("/screener/report/job-1")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="screener-detail-page"' in body
    assert 'id="detail-status-panel"' in body
    assert 'id="detail-order-form"' in body
    assert 'id="detail-order-side-tabs"' in body
    assert 'id="detail-order-side"' in body
    assert "job-1" in body
    assert "pollingEnabled" in body
    assert "nextErrorBackoffDelay" in body


def test_screener_dashboard_page_renders_nav_with_user(
    client_with_user: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, _ = client_with_user
    response = test_client.get("/screener")

    assert response.status_code == 200
    body = response.text
    assert '<nav class="navbar' in body
    assert 'href="/screener"' in body
    assert 'href="/portfolio/"' in body


def test_screener_report_page_renders_nav_with_user(
    client_with_user: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, _ = client_with_user
    response = test_client.get("/screener/report/job-1")

    assert response.status_code == 200
    body = response.text
    assert '<nav class="navbar' in body
    assert 'href="/screener"' in body
    assert 'href="/portfolio/"' in body


def test_list_endpoint_calls_service(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.get(
        "/api/screener/list",
        params={"market": "us", "max_rsi": 40, "min_volume": 1000, "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()["market"] == "us"
    fake_service.list_screening.assert_awaited_once()
    list_await_args = fake_service.list_screening.await_args
    assert list_await_args is not None
    assert list_await_args.kwargs == {
        "market": "us",
        "asset_type": None,
        "category": None,
        "strategy": None,
        "sort_by": None,
        "sort_order": "desc",
        "min_market_cap": None,
        "max_per": None,
        "max_pbr": None,
        "min_dividend_yield": None,
        "max_rsi": 40.0,
        "min_volume": 1000.0,
        "limit": 10,
    }


def test_list_endpoint_returns_400_for_validation_error() -> None:
    app = FastAPI()
    fake_service = _FakeScreenerService()
    fake_service.list_screening = AsyncMock(
        side_effect=ValueError(
            "Crypto market does not support sorting by 'volume'; use 'trade_amount'"
        )
    )
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/api/screener/list",
            params={"market": "crypto", "sort_by": "volume"},
        )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Crypto market does not support sorting by 'volume'; use 'trade_amount'"
    )


def test_refresh_endpoint_calls_service(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.post(
        "/api/screener/refresh",
        json={"market": "us", "max_rsi": 50, "min_volume": 2500, "limit": 5},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["code"] == "MSFT"
    fake_service.refresh_screening.assert_awaited_once()
    refresh_await_args = fake_service.refresh_screening.await_args
    assert refresh_await_args is not None
    assert refresh_await_args.kwargs == {
        "market": "us",
        "asset_type": None,
        "category": None,
        "strategy": None,
        "sort_by": None,
        "sort_order": "desc",
        "min_market_cap": None,
        "max_per": None,
        "max_pbr": None,
        "min_dividend_yield": None,
        "max_rsi": 50.0,
        "min_volume": 2500.0,
        "limit": 5,
    }


def test_list_endpoint_returns_400_for_negative_min_volume() -> None:
    app = FastAPI()
    fake_service = _FakeScreenerService()
    fake_service.list_screening = AsyncMock(
        side_effect=ValueError("min_volume must be >= 0")
    )
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/api/screener/list",
            params={"market": "us", "min_volume": -1},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "min_volume must be >= 0"


def test_list_endpoint_uses_default_limit_50(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.get("/api/screener/list", params={"market": "us"})

    assert response.status_code == 200
    fake_service.list_screening.assert_awaited_once()
    list_await_args = fake_service.list_screening.await_args
    assert list_await_args is not None
    assert list_await_args.kwargs["limit"] == 50


def test_list_endpoint_rejects_limit_over_100() -> None:
    app = FastAPI()
    fake_service = _FakeScreenerService()
    app.include_router(screener.router)
    app.dependency_overrides[screener.get_screener_service] = lambda: fake_service

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/screener/list", params={"limit": 101})

    assert response.status_code == 422


def test_report_request_endpoint(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.post(
        "/api/screener/report",
        json={"market": "us", "symbol": "AAPL", "name": "Apple"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["status"] == "queued"
    fake_service.request_report.assert_awaited_once_with(
        market="us", symbol="AAPL", name="Apple"
    )


def test_report_status_endpoint(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.get("/api/screener/report/job-1")
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    fake_service.get_report_status.assert_awaited_once_with("job-1")


def test_order_endpoint_calls_service(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.post(
        "/api/screener/order",
        json={
            "market": "us",
            "symbol": "AAPL",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "price": 100,
            "confirm": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    fake_service.place_order.assert_awaited_once()
