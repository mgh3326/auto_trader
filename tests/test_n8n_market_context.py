from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_market_context_with_btc_dominance(
        self,
        client: TestClient,
    ) -> None:
        """Test that market context endpoint returns BTC dominance data."""
        with (
            patch(
                "app.services.n8n_market_context_service.fetch_btc_dominance",
            ) as mock_btc,
            patch(
                "app.services.n8n_market_context_service.fetch_fear_greed",
            ) as mock_fg,
            patch(
                "app.services.n8n_market_context_service.fetch_economic_events_today",
            ) as mock_econ,
        ):
            mock_btc.return_value = {
                "btc_dominance": 61.5,
                "total_market_cap_change_24h": 2.3,
            }
            mock_fg.return_value = {
                "value": 45,
                "label": "Neutral",
                "previous": 42,
                "trend": "improving",
            }
            mock_econ.return_value = []

            response = client.get("/api/n8n/market-context")
            assert response.status_code == 200
            data = response.json()

            assert data["market_overview"]["btc_dominance"] == 61.5
            assert data["market_overview"]["total_market_cap_change_24h"] == 2.3

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_market_context_with_economic_events(
        self,
        client: TestClient,
    ) -> None:
        """Test that market context endpoint returns economic events."""
        with (
            patch(
                "app.services.n8n_market_context_service.fetch_btc_dominance",
            ) as mock_btc,
            patch(
                "app.services.n8n_market_context_service.fetch_fear_greed",
            ) as mock_fg,
            patch(
                "app.services.n8n_market_context_service.fetch_economic_events_today",
            ) as mock_econ,
        ):
            mock_btc.return_value = {
                "btc_dominance": 61.5,
                "total_market_cap_change_24h": 2.3,
            }
            mock_fg.return_value = {
                "value": 45,
                "label": "Neutral",
                "previous": 42,
                "trend": "improving",
            }
            mock_econ.return_value = [
                {
                    "time": "21:30 KST",
                    "event": "US CPI",
                    "importance": "high",
                    "previous": "2.4%",
                    "forecast": "2.3%",
                }
            ]

            response = client.get("/api/n8n/market-context")
            assert response.status_code == 200
            data = response.json()

            assert "economic_events_today" in data["market_overview"]
            events = data["market_overview"]["economic_events_today"]
            assert len(events) >= 1

            first_event = events[0]
            assert "time" in first_event
            assert "event" in first_event
            assert "importance" in first_event

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
    async def test_fetch_economic_events_today_uses_fifteen_minute_cache_on_provider_failure(
        self,
    ) -> None:
        from datetime import timedelta

        from app.core.timezone import now_kst
        from app.services.external import economic_calendar

        economic_calendar._clear_economic_calendar_cache()
        before = now_kst()

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            side_effect=Exception("Provider failure"),
        ):
            result = await economic_calendar.fetch_economic_events_today()

        assert result == []
        ttl = economic_calendar._econ_calendar_cache_expires - before
        assert timedelta(minutes=14) <= ttl <= timedelta(minutes=16)

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_uses_fifteen_minute_cache_on_malformed_xml(
        self,
    ) -> None:
        from datetime import timedelta

        from app.core.timezone import now_kst
        from app.services.external import economic_calendar

        economic_calendar._clear_economic_calendar_cache()
        before = now_kst()

        response = MagicMock()
        response.text = "<weeklyevents><event><title>broken"
        response.raise_for_status.return_value = None

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            return_value=response,
        ):
            result = await economic_calendar.fetch_economic_events_today()

        assert result == []
        ttl = economic_calendar._econ_calendar_cache_expires - before
        assert timedelta(minutes=14) <= ttl <= timedelta(minutes=16)

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_uses_one_hour_cache_for_valid_empty_result(
        self,
    ) -> None:
        from datetime import timedelta

        from app.core.timezone import now_kst
        from app.services.external import economic_calendar

        economic_calendar._clear_economic_calendar_cache()
        before = now_kst()

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            return_value=[],
        ):
            result = await economic_calendar.fetch_economic_events_today()

        assert result == []
        ttl = economic_calendar._econ_calendar_cache_expires - before
        assert timedelta(minutes=59) <= ttl <= timedelta(minutes=61)

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_empty_is_valid(self) -> None:
        """Test that empty provider response is treated as valid (no events today)."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            return_value=[],
        ):
            result = await fetch_economic_events_today()
            assert result == []
            assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_caches_result(self) -> None:
        """Test that successful fetch is cached and not re-fetched."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        mock_events = [
            {
                "time": "22:30 KST",
                "event": "Core CPI m/m",
                "country": "USD",
                "impact": "high",
                "previous": "0.4%",
                "forecast": "0.3%",
                "actual": None,
            }
        ]

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            return_value=mock_events,
        ) as mock_fetch:
            result1 = await fetch_economic_events_today()
            result2 = await fetch_economic_events_today()

            assert len(result1) == 1
            assert len(result2) == 1
            # Should only call provider once due to caching
            assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_maps_impact_to_importance(self) -> None:
        """Test that ForexFactory impact is mapped to importance."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        mock_events = [
            {
                "time": "22:30 KST",
                "event": "High Impact",
                "country": "USD",
                "impact": "high",
                "previous": None,
                "forecast": None,
                "actual": None,
            },
            {
                "time": "23:00 KST",
                "event": "Low Impact",
                "country": "USD",
                "impact": "low",
                "previous": None,
                "forecast": None,
                "actual": None,
            },
        ]

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            return_value=mock_events,
        ):
            result = await fetch_economic_events_today()

            assert len(result) == 2
            assert result[0]["importance"] == "high"
            assert result[1]["importance"] == "low"

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
    async def test_fetch_fear_greed_handles_error(self) -> None:
        """Test Fear & Greed fetch handles API errors gracefully."""
        from app.services.external.fear_greed import fetch_fear_greed

        with patch(
            "app.services.external.fear_greed.httpx.AsyncClient.get",
            side_effect=Exception("Network error"),
        ):
            result = await fetch_fear_greed()

            assert result is None


@pytest.mark.unit
class TestBtcDominanceService:
    """Tests for BTC dominance service."""

    @pytest.mark.asyncio
    async def test_fetch_btc_dominance_success(self) -> None:
        """Test successful BTC dominance fetch."""
        from app.services.external.btc_dominance import (
            _clear_btc_dominance_cache,
            fetch_btc_dominance,
        )

        _clear_btc_dominance_cache()

        mock_response = {
            "data": {
                "market_cap_percentage": {"btc": 61.2, "eth": 12.5},
                "market_cap_change_percentage_24h_usd": 2.3,
            }
        }

        with patch(
            "app.services.external.btc_dominance.httpx.AsyncClient.get",
            return_value=MagicMock(
                raise_for_status=lambda: None,
                json=lambda: mock_response,
            ),
        ):
            result = await fetch_btc_dominance()
            assert result is not None
            assert result["btc_dominance"] == 61.2
            assert result["total_market_cap_change_24h"] == 2.3

    @pytest.mark.asyncio
    async def test_fetch_btc_dominance_handles_error(self) -> None:
        """Test BTC dominance fetch handles API errors."""
        from app.services.external.btc_dominance import (
            _clear_btc_dominance_cache,
            fetch_btc_dominance,
        )

        _clear_btc_dominance_cache()

        with patch(
            "app.services.external.btc_dominance.httpx.AsyncClient.get",
            side_effect=Exception("Network error"),
        ):
            result = await fetch_btc_dominance()
            assert result is None


@pytest.mark.unit
class TestMarketContextSchemas:
    """Tests for Pydantic schemas."""

    def test_market_context_response_validates(self) -> None:
        """Test that response schema validates correctly."""
        from app.schemas.n8n.market_context import N8nMarketContextResponse

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


@pytest.mark.live
@pytest.mark.integration
class TestEconomicCalendarLive:
    """Live tests that hit real ForexFactory XML feed — require --run-live flag."""

    @pytest.mark.asyncio
    async def test_forexfactory_returns_events_today(self) -> None:
        """Verify ForexFactory provider returns structured events for today."""
        from app.services.external.forexfactory_calendar import (
            fetch_forexfactory_events_today,
        )

        result = await fetch_forexfactory_events_today()

        # Result must be a list (not None, not exception)
        assert isinstance(result, list)

        if result:
            first = result[0]
            assert "event" in first
            assert "country" in first
            assert "impact" in first
            assert "time" in first
            assert "KST" in first["time"]


@pytest.mark.unit
class TestEconomicCalendarDiagnostics:
    """Tests for economic calendar diagnostic improvements."""

    @pytest.mark.asyncio
    async def test_fetch_logs_warning_on_provider_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify that a warning is logged when provider fails."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            side_effect=Exception("Provider failure"),
        ):
            result = await fetch_economic_events_today()
            assert result == []
            assert "Failed to fetch economic calendar" in caplog.text

    @pytest.mark.asyncio
    async def test_fetch_logs_event_count_on_success(self) -> None:
        """Verify event count is logged on success."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        mock_events = [
            {
                "time": "22:30 KST",
                "event": "CPI",
                "country": "USD",
                "impact": "high",
                "previous": "0.4%",
                "forecast": "0.3%",
                "actual": None,
            },
            {
                "time": "23:00 KST",
                "event": "FOMC",
                "country": "USD",
                "impact": "high",
                "previous": None,
                "forecast": None,
                "actual": None,
            },
        ]

        with patch(
            "app.services.external.economic_calendar.fetch_forexfactory_events_today",
            return_value=mock_events,
        ):
            result = await fetch_economic_events_today()
            assert len(result) == 2
