from __future__ import annotations

from collections.abc import Generator
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


def test_screener_dashboard_page(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, _ = client
    response = test_client.get("/screener")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="screener-main-page"' in body
    assert 'id="filter-form"' in body
    assert 'id="results-table"' in body
    assert 'id="report-panel"' in body
    assert 'id="order-form"' in body
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
    assert "job-1" in body
    assert "pollingEnabled" in body
    assert "nextErrorBackoffDelay" in body


def test_list_endpoint_calls_service(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.get(
        "/api/screener/list",
        params={"market": "us", "max_rsi": 40, "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()["market"] == "us"
    fake_service.list_screening.assert_awaited_once()


def test_refresh_endpoint_calls_service(
    client: tuple[TestClient, _FakeScreenerService],
) -> None:
    test_client, fake_service = client

    response = test_client.post(
        "/api/screener/refresh",
        json={"market": "us", "max_rsi": 50, "limit": 5},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["code"] == "MSFT"
    fake_service.refresh_screening.assert_awaited_once()


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
