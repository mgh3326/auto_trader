"""Tests for n8n scan API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """Build test client with auth middleware bypassed."""
    # Monkeypatch settings before importing/creating app
    import app.middleware.auth

    monkeypatch.setattr(app.middleware.auth.settings, "N8N_API_KEY", "test-key")
    monkeypatch.setattr(app.middleware.auth.settings, "DOCS_ENABLED", False)
    monkeypatch.setattr(app.middleware.auth.settings, "PUBLIC_API_PATHS", [])

    from app.main import create_app

    app = create_app()
    test_client = TestClient(app)
    test_client.headers.update({"X-N8N-API-KEY": "test-key"})
    return test_client


@pytest.mark.unit
class TestStrategyScanEndpoint:
    def test_strategy_scan_success(self, client, monkeypatch):
        mock_result = {
            "alerts_sent": 1,
            "message": "🔎 크립토 스캔 (07:30)\n📌 BTC 컨텍스트: RSI14 63.5",
            "details": {
                "buy_signals": ["📉 TEST RSI 29.8"],
                "sell_signals": [],
                "sentiment_signals": [],
                "btc_context": "📌 BTC 컨텍스트: RSI14 63.5",
            },
        }
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["scan_type"] == "strategy"
        assert body["alerts_sent"] == 1
        assert body["message"]
        assert body["details"]["buy_signals"] == pytest.approx(["📉 TEST RSI 29.8"])

    def test_strategy_scan_no_signals(self, client):
        mock_result = {
            "alerts_sent": 0,
            "message": "",
            "details": {
                "buy_signals": [],
                "sell_signals": [],
                "sentiment_signals": [],
                "btc_context": "📌 BTC 컨텍스트: RSI14 63.5",
            },
        }
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["alerts_sent"] == 0

    def test_strategy_scan_exception_returns_500(self, client):
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_strategy_scan = AsyncMock(
                side_effect=RuntimeError("upstream failure")
            )
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/strategy")

        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert body["errors"]


@pytest.mark.unit
class TestCrashScanEndpoint:
    def test_crash_scan_success(self, client):
        mock_result = {
            "alerts_sent": 1,
            "message": "크래시 감지 스캔 (05:00)\n\n변동성 경보\n- TEST 24h +11.00%",
            "details": {
                "crash_signals": ["TEST 24h +11.00% — 급등 감지"],
            },
        }
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["scan_type"] == "crash_detection"
        assert body["alerts_sent"] == 1
        assert body["details"]["crash_signals"]

    def test_crash_scan_no_alerts(self, client):
        mock_result = {
            "alerts_sent": 0,
            "message": "",
            "details": {"crash_signals": []},
        }
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(return_value=mock_result)
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 200
        body = resp.json()
        assert body["alerts_sent"] == 0

    def test_crash_scan_exception_returns_500(self, client):
        with patch("app.routers.n8n_scan.DailyScanner") as MockScanner:
            instance = MockScanner.return_value
            instance.run_crash_detection = AsyncMock(
                side_effect=RuntimeError("upstream failure")
            )
            instance.close = AsyncMock()

            resp = client.get("/api/n8n/scan/crash")

        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
