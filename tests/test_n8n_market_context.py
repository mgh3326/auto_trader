from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.timezone import now_kst
from app.routers.n8n import router as n8n_router


@pytest.fixture
def app() -> FastAPI:
    """Create test FastAPI app with n8n router."""
    app = FastAPI()
    app.include_router(n8n_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create test client."""
    return TestClient(app)


@pytest.mark.unit
class TestMarketContextEndpoint:
    """Tests for /api/n8n/market-context endpoint."""

    def test_endpoint_returns_success(self, client: TestClient) -> None:
        """Test that endpoint returns success response."""
        with patch("app.routers.n8n.fetch_market_context") as mock_fetch:
            mock_fetch.return_value = {
                "market_overview": {
                    "fear_greed": None,
                    "btc_dominance": None,
                    "total_market_cap_change_24h": None,
                    "economic_events_today": [],
                },
                "symbols": [],
                "summary": {
                    "total_symbols": 0,
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                    "avg_rsi": None,
                    "market_sentiment": "neutral",
                },
                "errors": [],
            }

            response = client.get("/api/n8n/market-context")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "as_of" in data
            assert data["market"] == "crypto"

    def test_endpoint_accepts_symbols_param(self, client: TestClient) -> None:
        """Test that endpoint accepts comma-separated symbols."""
        with patch("app.routers.n8n.fetch_market_context") as mock_fetch:
            mock_fetch.return_value = {
                "market_overview": {
                    "fear_greed": None,
                    "btc_dominance": None,
                    "total_market_cap_change_24h": None,
                    "economic_events_today": [],
                },
                "symbols": [],
                "summary": {
                    "total_symbols": 0,
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                    "avg_rsi": None,
                    "market_sentiment": "neutral",
                },
                "errors": [],
            }

            response = client.get("/api/n8n/market-context?symbols=BTC,ETH,SOL")
            assert response.status_code == 200

            call_kwargs = mock_fetch.call_args.kwargs
            assert call_kwargs["symbols"] == ["BTC", "ETH", "SOL"]

    def test_endpoint_disables_fear_greed_when_requested(
        self, client: TestClient
    ) -> None:
        """Test that include_fear_greed=false is respected."""
        with patch("app.routers.n8n.fetch_market_context") as mock_fetch:
            mock_fetch.return_value = {
                "market_overview": {
                    "fear_greed": None,
                    "btc_dominance": None,
                    "total_market_cap_change_24h": None,
                    "economic_events_today": [],
                },
                "symbols": [],
                "summary": {
                    "total_symbols": 0,
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                    "avg_rsi": None,
                    "market_sentiment": "neutral",
                },
                "errors": [],
            }

            response = client.get("/api/n8n/market-context?include_fear_greed=false")
            assert response.status_code == 200

            call_kwargs = mock_fetch.call_args.kwargs
            assert call_kwargs["include_fear_greed"] is False

    def test_endpoint_handles_service_error(self, client: TestClient) -> None:
        """Test that endpoint returns 500 on service error."""
        with patch("app.routers.n8n.fetch_market_context") as mock_fetch:
            mock_fetch.side_effect = Exception("Service failure")

            response = client.get("/api/n8n/market-context")
            assert response.status_code == 500
            data = response.json()
            assert data["success"] is False
            assert len(data["errors"]) > 0


@pytest.mark.unit
class TestMarketContextService:
    """Tests for market context service functions."""

    @pytest.mark.asyncio
    async def test_classify_trend_bullish(self) -> None:
        """Test bullish trend classification."""
        from app.services.n8n_market_context_service import _classify_trend

        result = _classify_trend(rsi_14=60.0, ema_distance_pct=5.0)
        assert result == "bullish"

    @pytest.mark.asyncio
    async def test_classify_trend_bearish(self) -> None:
        """Test bearish trend classification."""
        from app.services.n8n_market_context_service import _classify_trend

        result = _classify_trend(rsi_14=40.0, ema_distance_pct=-5.0)
        assert result == "bearish"

    @pytest.mark.asyncio
    async def test_classify_trend_neutral(self) -> None:
        """Test neutral trend classification."""
        from app.services.n8n_market_context_service import _classify_trend

        result = _classify_trend(rsi_14=50.0, ema_distance_pct=1.0)
        assert result == "neutral"

        result = _classify_trend(rsi_14=None, ema_distance_pct=5.0)
        assert result == "neutral"

    @pytest.mark.asyncio
    async def test_classify_strength(self) -> None:
        """Test trend strength classification."""
        from app.services.n8n_market_context_service import _classify_strength

        assert _classify_strength(45.0) == "strong"

        assert _classify_strength(30.0) == "moderate"

        assert _classify_strength(20.0) == "weak"

        assert _classify_strength(None) == "weak"

    @pytest.mark.asyncio
    async def test_normalize_crypto_symbol(self) -> None:
        """Test crypto symbol normalization."""
        from app.services.n8n_market_context_service import _normalize_crypto_symbol

        assert _normalize_crypto_symbol("BTC") == "KRW-BTC"

        assert _normalize_crypto_symbol("KRW-BTC") == "KRW-BTC"
        assert _normalize_crypto_symbol("USDT-BTC") == "USDT-BTC"

        assert _normalize_crypto_symbol("btc") == "KRW-BTC"

        assert _normalize_crypto_symbol("") == ""


@pytest.mark.unit
class TestFearGreedService:
    """Tests for Fear & Greed service."""

    @pytest.mark.asyncio
    async def test_fetch_fear_greed_returns_data(self) -> None:
        """Test Fear & Greed fetch returns proper structure."""
        from app.services.external.fear_greed import fetch_fear_greed

        mock_response = {
            "data": [
                {"value": "34", "value_classification": "Fear"},
                {"value": "28", "value_classification": "Fear"},
            ]
        }

        with patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value.json = AsyncMock(return_value=mock_response)
            mock_get.return_value.raise_for_status = AsyncMock()

            result = await fetch_fear_greed()

            assert result is not None
            assert result["value"] == 34
            assert result["label"] == "Fear"
            assert result["previous"] == 28
            assert result["trend"] == "improving"

    @pytest.mark.asyncio
    async def test_fetch_fear_greed_handles_error(self) -> None:
        """Test Fear & Greed fetch handles API errors gracefully."""
        from app.services.external.fear_greed import fetch_fear_greed

        with patch("httpx.AsyncClient.get") as mock_get:
            mock_get.side_effect = Exception("Network error")

            result = await fetch_fear_greed()

            assert result is None


@pytest.mark.unit
class TestMarketContextSchemas:
    """Tests for Pydantic schemas."""

    def test_market_context_response_validates(self) -> None:
        """Test that response schema validates correctly."""
        from app.schemas.n8n import N8nMarketContextResponse

        data = {
            "success": True,
            "as_of": "2026-03-16T09:00:00+09:00",
            "market": "crypto",
            "market_overview": {
                "fear_greed": {
                    "value": 34,
                    "label": "Fear",
                    "previous": 28,
                    "trend": "improving",
                },
                "btc_dominance": 61.2,
                "total_market_cap_change_24h": 2.3,
                "economic_events_today": [],
            },
            "symbols": [
                {
                    "symbol": "BTC",
                    "raw_symbol": "KRW-BTC",
                    "current_price": 108600000,
                    "current_price_fmt": "1.09억",
                    "change_24h_pct": 3.2,
                    "change_24h_fmt": "+3.2%",
                    "volume_24h_krw": 285000000000,
                    "volume_24h_fmt": "2,850억",
                    "rsi_14": 61.1,
                    "rsi_7": 65.3,
                    "stoch_rsi_k": 72.5,
                    "adx": 28.3,
                    "ema_20_distance_pct": 4.2,
                    "trend": "bullish",
                    "trend_strength": "moderate",
                }
            ],
            "summary": {
                "total_symbols": 1,
                "bullish_count": 1,
                "bearish_count": 0,
                "neutral_count": 0,
                "avg_rsi": 61.1,
                "market_sentiment": "cautiously_bullish",
            },
            "errors": [],
        }

        response = N8nMarketContextResponse(**data)
        assert response.success is True
        assert response.market == "crypto"
        assert len(response.symbols) == 1
        assert response.symbols[0].symbol == "BTC"
