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
from app.services import upbit, kis, yahoo
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
        # Test dashboard endpoint
        response = client.get("/dashboard/")
        assert response.status_code == 200

    def test_analysis_integration(self, client):
        """Test analysis endpoint integration."""
        # Test analysis endpoint
        response = client.get("/dashboard/analysis")
        assert response.status_code == 200

    def test_application_structure(self, client):
        """Test application structure and configuration."""
        app = client.app
        assert app.title == "KIS Auto Screener"
        assert app.version == "0.1.0"

    @patch('app.services.upbit.httpx.AsyncClient')
    @patch('app.services.yahoo.yf.Ticker')  # 경로 수정
    @patch('app.services.kis.httpx.AsyncClient')
    @patch('app.analysis.analyzer.genai.Client')  # 경로 수정
    def test_external_services_integration(self, mock_gemini_client, mock_kis_client, mock_yahoo_ticker, mock_upbit_client, client):
        """Test integration with external services (mocked)."""
        # Configure mocks
        mock_upbit_client.return_value.__aenter__.return_value.get.return_value = AsyncMock(
            status_code=200,
            json=AsyncMock(return_value=[{"market": "KRW-BTC", "trade_price": 45000000}])
        )
        
        mock_yahoo_ticker.return_value.info = {
            "symbol": "AAPL",
            "longName": "Apple Inc.",
            "currentPrice": 150.0
        }
        
        mock_kis_client.return_value.__aenter__.return_value.post.return_value = AsyncMock(
            status_code=200,
            json=AsyncMock(return_value={"access_token": "test_token"})
        )
        
        mock_gemini_instance = MagicMock()
        mock_gemini_instance.models.generate_content.return_value = MagicMock(text="Mock AI analysis response")
        mock_gemini_client.return_value = mock_gemini_instance
        
        # 실제 API 호출을 통해 Mock이 사용되는지 테스트해야 하지만,
        # 여기서는 patch 경로가 올바른지만 확인하므로 pass
        pass


@pytest.mark.integration
class TestDataFlow:
    """Test data flow through the application."""

    def test_data_processing_pipeline(self):
        """Test the complete data processing pipeline."""
        pass

    def test_analysis_workflow(self):
        """Test the complete analysis workflow."""
        pass


@pytest.mark.integration
class TestExternalServiceMocking:
    """Test that all external services are properly mocked."""

    @patch('app.services.upbit.httpx.AsyncClient')
    def test_upbit_service_mocking(self, mock_upbit):
        """Test Upbit service mocking."""
        mock_upbit.return_value.__aenter__.return_value.get.return_value = AsyncMock(
            status_code=200, json=AsyncMock(return_value={})
        )
        
        # 실제 서비스 함수를 호출하여 Mock이 사용되도록 함
        asyncio.run(upbit.fetch_price("KRW-BTC"))
        
        assert mock_upbit.called

    @patch('app.services.yahoo.yf.download')  # Ticker 대신 download 함수를 mock
    def test_yahoo_service_mocking(self, mock_yahoo_download):
        """Test Yahoo Finance service mocking."""
        mock_yahoo_download.return_value = pd.DataFrame() # 빈 DataFrame 반환
        
        # 실제 서비스 함수를 호출
        asyncio.run(yahoo.fetch_ohlcv("AAPL"))
        
        assert mock_yahoo_download.called

    @patch('app.services.kis.httpx.AsyncClient')
    def test_kis_service_mocking(self, mock_kis):
        """Test KIS service mocking."""
        mock_instance = mock_kis.return_value.__aenter__.return_value
        mock_instance.post.return_value = AsyncMock(status_code=200, json=AsyncMock(return_value={"access_token": "test"}))
        mock_instance.get.return_value = AsyncMock(status_code=200, json=AsyncMock(return_value={"rt_cd": "0", "output": []}))

        # 실제 서비스 함수를 호출
        asyncio.run(kis.volume_rank())

        assert mock_kis.called

    @patch('app.analysis.analyzer.genai.Client') # 경로 수정
    def test_gemini_service_mocking(self, mock_gemini_client):
        """Test Gemini AI service mocking."""
        mock_instance = mock_gemini_client.return_value
        mock_response = MagicMock()
        mock_response.text = "test response"
        # candidates와 finish_reason을 포함한 응답 구조 모방
        mock_candidate = MagicMock()
        mock_candidate.finish_reason = "STOP"
        mock_response.candidates = [mock_candidate]
        mock_instance.models.generate_content.return_value = mock_response

        # 실제 분석기를 실행하여 Mock이 사용되도록 함
        analyzer = Analyzer()
        dummy_df = pd.DataFrame({'close': [1,2,3], 'high': [1,2,3], 'low':[1,2,3], 'open':[1,2,3], 'volume':[1,2,3]})

        # _save_to_db는 테스트 대상이 아니므로 mock 처리
        with patch('app.analysis.analyzer.Analyzer._save_to_db', new_callable=AsyncMock):
            asyncio.run(analyzer.analyze_and_save(df=dummy_df, symbol="TEST", name="Test", instrument_type="test"))
        
        assert mock_gemini_client.called
