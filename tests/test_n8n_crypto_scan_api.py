"""Tests for GET /api/n8n/crypto-scan endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.n8n import router as n8n_router


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(n8n_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _mock_scan_result(**overrides) -> dict:
    """Build a valid scan result dict."""
    base = {
        "success": True,
        "btc_context": {
            "rsi14": 63.5,
            "sma20": 101_000_000.0,
            "sma60": 109_000_000.0,
            "sma200": 137_000_000.0,
            "current_price": 110_000_000,
            "change_rate_24h": -0.0018,
        },
        "fear_greed": {
            "value": 34,
            "label": "Fear",
            "previous": 28,
            "trend": "improving",
        },
        "coins": [
            {
                "symbol": "KRW-BTC",
                "currency": "BTC",
                "name": "비트코인",
                "rank": 1,
                "is_holding": True,
                "current_price": 110_000_000,
                "change_rate_24h": -0.0018,
                "trade_amount_24h": 150_000_000_000,
                "indicators": {
                    "rsi14": 63.5,
                    "sma20": 101_000_000.0,
                    "sma60": 109_000_000.0,
                    "sma200": 137_000_000.0,
                },
                "sma_cross": None,
                "crash": None,
            }
        ],
        "summary": {
            "total_scanned": 30,
            "top_n_count": 30,
            "holdings_added": 0,
            "oversold_count": 2,
            "overbought_count": 0,
            "crash_triggered_count": 0,
            "sma_golden_cross_count": 1,
            "sma_dead_cross_count": 0,
        },
        "errors": [],
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestCryptoScanEndpoint:
    """Tests for GET /api/n8n/crypto-scan."""

    def test_success_response(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "as_of" in data
        assert "scan_params" in data
        assert data["scan_params"]["top_n"] == 30
        assert len(data["coins"]) == 1
        assert data["coins"][0]["symbol"] == "KRW-BTC"

    def test_query_params_forwarded(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get(
                "/api/n8n/crypto-scan",
                params={
                    "top_n": 10,
                    "include_holdings": False,
                    "include_crash": False,
                    "include_sma_cross": False,
                    "include_fear_greed": False,
                    "ohlcv_days": 100,
                },
            )

        assert response.status_code == 200
        mock_fetch.assert_called_once_with(
            top_n=10,
            include_holdings=False,
            include_crash=False,
            include_sma_cross=False,
            include_fear_greed=False,
            ohlcv_days=100,
        )
        data = response.json()
        assert data["scan_params"]["top_n"] == 10
        assert data["scan_params"]["include_holdings"] is False
        assert data["scan_params"]["ohlcv_days"] == 100

    def test_service_exception_returns_500(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = RuntimeError("Upbit down")
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) >= 1

    def test_top_n_validation_min(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"top_n": 0})
        assert response.status_code == 422

    def test_top_n_validation_max(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"top_n": 200})
        assert response.status_code == 422

    def test_ohlcv_days_validation(self, client: TestClient) -> None:
        response = client.get("/api/n8n/crypto-scan", params={"ohlcv_days": 5})
        assert response.status_code == 422

    def test_default_params(self, client: TestClient) -> None:
        with patch(
            "app.routers.n8n.fetch_crypto_scan", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _mock_scan_result()
            response = client.get("/api/n8n/crypto-scan")

        assert response.status_code == 200
        data = response.json()
        assert data["scan_params"]["top_n"] == 30
        assert data["scan_params"]["include_holdings"] is True
        assert data["scan_params"]["include_crash"] is True
        assert data["scan_params"]["include_sma_cross"] is True
        assert data["scan_params"]["include_fear_greed"] is True
        assert data["scan_params"]["ohlcv_days"] == 50
