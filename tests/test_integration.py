"""
Integration tests for auto-trader application.
"""
import asyncio
import pytest
import pandas as pd
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

from app.main import api
# 테스트를 위해 서비스 및 분석기 임포트
from app.services import upbit, yahoo
from app.services.kis import kis as kis_client  # kis 인스턴스를 직접 임포트
from app.analysis.analyzer import Analyzer


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

    def test_dashboard_integration(self, client):
        """Test dashboard integration."""
        response = client.get("/dashboard/")
        assert response.status_code == 200

    def test_analysis_integration(self, client):
        """Test analysis endpoint integration."""
        response = client.get("/dashboard/analysis")
        assert response.status_code == 200

    def test_application_structure(self, client):
        """Test application structure and configuration."""
        app = client.app
        assert app.title == "KIS Auto Screener"
        assert app.version == "0.1.0"

    @patch('app.services.upbit.httpx.AsyncClient')
    @patch('app.services.yahoo.yf.Ticker')
    @patch('app.services.kis.httpx.AsyncClient')
    @patch('app.analysis.analyzer.genai.Client')
    def test_external_services_integration(self, mock_gemini_client, mock_kis_client, mock_yahoo_ticker, mock_upbit_client, client):
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
    @patch('app.services.upbit.httpx.AsyncClient')
    async def test_upbit_service_mocking(self, mock_upbit):
        """Test Upbit service mocking."""
        mock_response_data = [{
            "opening_price": 45000000, "high_price": 46000000,
            "low_price": 44000000, "trade_price": 45500000,
            "acc_trade_volume_24h": 100.0, "acc_trade_price_24h": 4550000000.0
        }]
        mock_response = MagicMock()
        mock_response.json.return_value = mock_response_data
        mock_upbit.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)

        await upbit.fetch_price("KRW-BTC")

        assert mock_upbit.called

    @pytest.mark.asyncio
    @patch('app.services.yahoo.yf.download')
    async def test_yahoo_service_mocking(self, mock_yahoo_download):
        """Test Yahoo Finance service mocking."""
        mock_df = pd.DataFrame({
            'Open': [100], 'High': [105], 'Low': [95],
            'Close': [103], 'Volume': [1000]
        })
        # yfinance는 DatetimeIndex를 반환하므로 이를 모방합니다.
        mock_df.index = pd.to_datetime(['2023-01-01'])
        mock_df.index.name = 'Date'
        mock_yahoo_download.return_value = mock_df

        await yahoo.fetch_ohlcv("AAPL")

        assert mock_yahoo_download.called

    @pytest.mark.asyncio
    @patch('app.services.kis.load_token', return_value='dummy_token')
    @patch('app.services.kis.httpx.AsyncClient')
    async def test_kis_service_mocking(self, mock_kis, mock_load_token):
        """Test KIS service mocking."""
        mock_instance = mock_kis.return_value.__aenter__.return_value
        mock_instance.get.return_value = MagicMock(json=lambda: {"rt_cd": "0", "output": []})

        await kis_client.volume_rank()

        assert mock_kis.called

    @pytest.mark.asyncio
    @patch('app.analysis.analyzer.Analyzer._save_to_db', new_callable=AsyncMock)
    @patch('app.analysis.analyzer.genai.Client')
    async def test_gemini_service_mocking(self, mock_gemini_client, mock_save_db):
        """Test Gemini AI service mocking."""
        mock_instance = mock_gemini_client.return_value
        mock_response = MagicMock()
        mock_response.text = "test response"
        mock_candidate = MagicMock()
        mock_candidate.finish_reason = "STOP"
        mock_response.candidates = [mock_candidate]
        mock_instance.models.generate_content.return_value = mock_response

        analyzer = Analyzer()
        # 'date' 컬럼을 포함한 dummy_df 생성
        dummy_df = pd.DataFrame({
            'date': pd.to_datetime(['2023-01-01', '2023-01-02', '2023-01-03']),
            'close': [1,2,3], 'high': [1,2,3],
            'low':[1,2,3], 'open':[1,2,3], 'volume':[1,2,3]
        })

        await analyzer.analyze_and_save(df=dummy_df, symbol="TEST", name="Test", instrument_type="test")

        assert mock_gemini_client.called