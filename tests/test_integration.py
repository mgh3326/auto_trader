"""
Integration tests for auto-trader application.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.brokers.upbit.client as upbit
import app.services.brokers.yahoo.client as yahoo
from app.main import api
from app.services.brokers.kis.client import (
    kis as kis_client,  # kis 인스턴스를 직접 임포트
)


@pytest.mark.integration
class TestApplicationIntegration:
    """Integration tests for the entire application."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(api)

    def test_application_startup(self, client):
        """Test that the application starts up correctly."""
        response = client.get("/healthz")
        assert response.status_code == 200

    def test_active_surface_integration(self, client):
        """Test active surface pages integration."""
        response = client.get("/screener")
        assert response.status_code == 200

        response = client.get("/portfolio/")
        assert response.status_code == 200

    def test_deprecated_dashboard_returns_410(self, client):
        """Test deprecated dashboard returns 410 Gone."""
        response = client.get("/dashboard/")
        assert response.status_code == 410

    def test_application_structure(self, client):
        """Test application structure and configuration."""
        app = client.app
        assert app.title == "KIS Auto Screener"
        assert app.version == "0.2.0"

    @patch("app.services.brokers.upbit.client.httpx.AsyncClient")
    @patch("app.services.brokers.yahoo.client.yf.Ticker")
    @patch("httpx.AsyncClient")
    def test_external_services_integration(
        self,
        mock_kis_client,
        mock_yahoo_ticker,
        mock_upbit_client,
        client,
    ):
        """Test integration with external services (mocked)."""
        # 이 테스트는 각 서비스의 Mocking이 올바르게 설정될 수 있는지 확인하는 것이 주 목적이므로,
        # 상세한 반환값 설정보다는 patch 경로의 유효성에 집중합니다.
        # 실제 동작 테스트는 TestExternalServiceMocking 클래스에서 수행합니다.
        pass


@pytest.mark.integration
class TestDataFlow:
    """Test data flow through the application."""

    def test_data_processing_pipeline(self):
        pass

    def test_analysis_workflow(self):
        pass


@pytest.mark.integration
class TestExternalServiceMocking:
    """Test that all external services are properly mocked."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client.httpx.AsyncClient")
    async def test_upbit_service_mocking(self, mock_upbit):
        """Test Upbit service mocking."""
        mock_response_data = [
            {
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "acc_trade_volume_24h": 100.0,
                "acc_trade_price_24h": 4550000000.0,
            }
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_upbit.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        await upbit.fetch_price("KRW-BTC")

        assert mock_upbit.called

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.download")
    async def test_yahoo_service_mocking(self, mock_yahoo_download, monkeypatch):
        """Test Yahoo Finance service mocking."""
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.settings.yahoo_ohlcv_cache_enabled",
            False,
            raising=False,
        )
        mock_df = pd.DataFrame(
            {
                "Open": [100],
                "High": [105],
                "Low": [95],
                "Close": [103],
                "Volume": [1000],
            }
        )
        mock_df.index = pd.to_datetime(["2023-01-01"])
        mock_df.index.name = "Date"
        mock_yahoo_download.return_value = mock_df

        await yahoo.fetch_ohlcv("AAPL")

        assert mock_yahoo_download.called

    @pytest.mark.asyncio
    @patch(
        "app.services.brokers.kis.client.KISClient._ensure_token",
        new_callable=AsyncMock,
    )
    @patch("httpx.AsyncClient")
    async def test_kis_service_mocking(self, mock_kis_client, mock_ensure_token):
        """Test KIS service mocking."""
        mock_instance = mock_kis_client.return_value.__aenter__.return_value

        # GET 요청(데이터 조회)에 대한 Mock 응답 설정
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {"rt_cd": "0", "output": []}
        mock_instance.get.return_value = mock_get_response

        await kis_client.volume_rank()

        assert mock_kis_client.called
        mock_ensure_token.assert_called_once()
        mock_instance.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_analysis_workflow_placeholder(self):
        pass
