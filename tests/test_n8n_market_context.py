from __future__ import annotations

from unittest.mock import MagicMock, patch

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
class TestFinnhubEconomicCalendar:
    """Tests for Finnhub economic calendar integration."""

    @pytest.mark.asyncio
    async def test_fetch_economic_calendar_success(self) -> None:
        """Test successful economic calendar fetch with real API response format."""
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        # Use real Finnhub response shape (dict wrapper + prev field)
        mock_response = {
            "economicCalendar": [
                {
                    "time": "08:30",
                    "country": "US",
                    "event": "CPI",
                    "actual": "2.4%",
                    "prev": "2.3%",
                    "estimate": "2.3%",
                    "impact": "high",
                },
                {
                    "time": "14:00",
                    "country": "US",
                    "event": "FOMC Statement",
                    "actual": None,
                    "prev": None,
                    "estimate": None,
                    "impact": "high",
                },
            ],
        }

        with patch(
            "app.mcp_server.tooling.fundamentals_sources_finnhub._get_finnhub_client",
        ) as mock_client:
            mock_instance = MagicMock()
            mock_instance.economic_calendar.return_value = mock_response
            mock_client.return_value = mock_instance

            result = await fetch_economic_calendar_finnhub("2026-03-16", "2026-03-16")

            assert result is not None
            assert len(result) == 2
            assert result[0]["event"] == "CPI"
            assert result[0]["country"] == "US"
            assert result[0]["previous"] == "2.3%"

    @pytest.mark.asyncio
    async def test_fetch_economic_calendar_handles_error(self) -> None:
        """Test economic calendar fetch handles API errors."""
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        with patch(
            "app.mcp_server.tooling.fundamentals_sources_finnhub._get_finnhub_client",
        ) as mock_client:
            mock_client.side_effect = Exception("API error")

            result = await fetch_economic_calendar_finnhub("2026-03-16", "2026-03-16")
            assert result is None

    @pytest.mark.asyncio
    async def test_fetch_economic_calendar_unwraps_dict_response(self) -> None:
        """Test that dict response with economicCalendar key is properly unwrapped."""
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        # Real Finnhub API response shape — dict with economicCalendar key
        mock_api_response = {
            "economicCalendar": [
                {
                    "time": "08:30:00",
                    "country": "US",
                    "event": "Initial Jobless Claims",
                    "actual": 220,
                    "prev": 215,
                    "estimate": 218,
                    "impact": "medium",
                    "unit": "K",
                },
                {
                    "time": "10:00:00",
                    "country": "US",
                    "event": "FOMC Statement",
                    "actual": None,
                    "prev": None,
                    "estimate": None,
                    "impact": "high",
                    "unit": "",
                },
                {
                    "time": "07:00:00",
                    "country": "DE",
                    "event": "German CPI",
                    "actual": 2.3,
                    "prev": 2.1,
                    "estimate": 2.2,
                    "impact": "high",
                    "unit": "%",
                },
            ],
        }

        with patch(
            "app.mcp_server.tooling.fundamentals_sources_finnhub._get_finnhub_client",
        ) as mock_client:
            mock_instance = MagicMock()
            mock_instance.economic_calendar.return_value = mock_api_response
            mock_client.return_value = mock_instance

            result = await fetch_economic_calendar_finnhub("2026-03-18", "2026-03-18")

            assert result is not None
            # Should have 2 US events (German event filtered out)
            assert len(result) == 2
            assert result[0]["event"] == "Initial Jobless Claims"
            assert result[1]["event"] == "FOMC Statement"
            # Verify field name normalization: prev → previous
            assert result[0]["previous"] == 215
            assert result[0]["estimate"] == 218


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
    async def test_is_high_importance_event(self) -> None:
        """Test high-importance event detection."""
        from app.services.external.economic_calendar import _is_high_importance_event

        assert _is_high_importance_event("US CPI") is True
        assert _is_high_importance_event("FOMC Meeting") is True
        assert _is_high_importance_event("Non-Farm Payrolls") is True
        assert _is_high_importance_event("GDP Growth") is True
        assert _is_high_importance_event("Retail Sales") is True
        assert _is_high_importance_event("Earnings Report") is False
        assert _is_high_importance_event("Dividend Announcement") is False

    @pytest.mark.asyncio
    async def test_convert_time_to_kst(self) -> None:
        """Test time conversion to KST."""
        from app.services.external.economic_calendar import _convert_time_to_kst

        assert _convert_time_to_kst("08:30") == "22:30 KST"
        assert _convert_time_to_kst("14:00") == "04:00 KST"
        assert _convert_time_to_kst("") == "00:00 KST"
        assert _convert_time_to_kst("invalid") == "00:00 KST"

    @pytest.mark.asyncio
    async def test_determine_importance(self) -> None:
        """Test importance level determination."""
        from app.services.external.economic_calendar import _determine_importance

        assert _determine_importance("CPI Release", None) == "high"
        assert _determine_importance("FOMC Statement", None) == "high"
        assert _determine_importance("Some Event", "low") == "low"
        assert _determine_importance("Some Event", "medium") == "medium"
        assert _determine_importance("Some Event", None) == "medium"

    @pytest.mark.asyncio
    async def test_normalize_crypto_symbol(self) -> None:
        """Test crypto symbol normalization."""
        from app.services.n8n_market_context_service import _normalize_crypto_symbol

        assert _normalize_crypto_symbol("BTC") == "KRW-BTC"

        assert _normalize_crypto_symbol("KRW-BTC") == "KRW-BTC"
        assert _normalize_crypto_symbol("USDT-BTC") == "USDT-BTC"

        assert _normalize_crypto_symbol("btc") == "KRW-BTC"

        assert _normalize_crypto_symbol("") == ""

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_maps_previous_correctly(
        self,
    ) -> None:
        """Test that previous values from Finnhub are correctly mapped."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        # Clear cache to force a fresh fetch
        _clear_economic_calendar_cache()

        mock_finnhub_events = [
            {
                "time": "08:30",
                "country": "US",
                "event": "CPI Release",
                "actual": None,
                "previous": "2.4%",
                "estimate": "2.3%",
                "impact": "high",
            },
        ]

        with patch(
            "app.services.external.economic_calendar.fetch_economic_calendar_finnhub",
        ) as mock_fetch:
            mock_fetch.return_value = mock_finnhub_events

            result = await fetch_economic_events_today()

            assert len(result) == 1
            assert result[0]["event"] == "CPI Release"
            assert result[0]["previous"] == "2.4%"
            assert result[0]["forecast"] == "2.3%"
            assert result[0]["importance"] == "high"
            assert result[0]["time"] == "22:30 KST"


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


@pytest.mark.live
@pytest.mark.integration
class TestEconomicCalendarLive:
    """Live tests that hit real Finnhub API — require --run-live flag."""

    @pytest.mark.asyncio
    async def test_finnhub_returns_events_for_known_date(self) -> None:
        """Verify Finnhub returns US economic events for a date with known events.

        Uses a recent historical date that definitely had events (e.g., first week of month
        typically has NFP, ISM PMI, etc.)
        """
        from app.mcp_server.tooling.fundamentals_sources_finnhub import (
            fetch_economic_calendar_finnhub,
        )

        # First Monday of March 2026 — ISM Manufacturing PMI is typically released
        result = await fetch_economic_calendar_finnhub("2026-03-02", "2026-03-06")

        assert result is not None, "Finnhub returned None — API key or connectivity issue"
        assert len(result) > 0, (
            "Finnhub returned 0 US events for first week of March — "
            "this week always has ISM PMI, likely an API or filter issue"
        )

        # Verify structure
        first_event = result[0]
        assert "event" in first_event
        assert "country" in first_event
        assert first_event["country"] == "US"

    @pytest.mark.asyncio
    async def test_fetch_economic_events_today_service_layer(self) -> None:
        """Verify end-to-end service layer returns structured events."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()
        result = await fetch_economic_events_today()

        # This test runs on whatever "today" is — can be empty
        # But the result must be a list (not None, not exception)
        assert isinstance(result, list)

        if result:
            first = result[0]
            assert "time" in first
            assert "event" in first
            assert "importance" in first
            assert first["importance"] in ("high", "medium", "low")
            assert "KST" in first["time"]


@pytest.mark.unit
class TestEconomicCalendarDiagnostics:
    """Tests for economic calendar diagnostic improvements."""

    @pytest.mark.asyncio
    async def test_fetch_logs_warning_when_finnhub_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify that a warning is logged when Finnhub returns None."""
        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        with patch(
            "app.services.external.economic_calendar.fetch_economic_calendar_finnhub",
            return_value=None,
        ):
            result = await fetch_economic_events_today()
            assert result == []
            assert "check FINNHUB_API_KEY and API connectivity" in caplog.text

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
                "time": "08:30",
                "event": "CPI",
                "previous": "2.4%",
                "estimate": "2.3%",
                "impact": "high",
            },
            {
                "time": "14:00",
                "event": "FOMC",
                "previous": None,
                "estimate": None,
                "impact": "high",
            },
        ]

        with patch(
            "app.services.external.economic_calendar.fetch_economic_calendar_finnhub",
            return_value=mock_events,
        ):
            result = await fetch_economic_events_today()
            assert len(result) == 2


@pytest.mark.unit
class TestEconomicCalendarDateBoundary:
    """Tests for KST/UTC date boundary handling."""

    @pytest.mark.asyncio
    async def test_kst_morning_includes_previous_utc_date(self) -> None:
        """At KST 07:00 (= UTC 22:00 previous day), Finnhub should query
        both the previous UTC day and current KST day to catch late-night
        UTC events that are "today" in KST.

        Example: KST 2026-03-19 07:00 = UTC 2026-03-18 22:00
        Finnhub query should cover 2026-03-18 to 2026-03-19 to catch
        events happening at UTC 2026-03-18 late evening.
        """
        from datetime import datetime
        from unittest.mock import AsyncMock, patch
        from zoneinfo import ZoneInfo

        from app.services.external.economic_calendar import (
            _clear_economic_calendar_cache,
            fetch_economic_events_today,
        )

        _clear_economic_calendar_cache()

        # Mock KST time to 2026-03-19 07:00 KST (= 2026-03-18 22:00 UTC)
        mock_now = datetime(2026, 3, 19, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))

        with (
            patch("app.services.external.economic_calendar.now_kst", return_value=mock_now),
            patch(
                "app.services.external.economic_calendar.fetch_economic_calendar_finnhub",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            mock_fetch.return_value = [
                {
                    "time": "14:00",
                    "event": "FOMC Rate Decision",
                    "previous": "5.25%",
                    "estimate": "5.25%",
                    "impact": "high",
                }
            ]

            result = await fetch_economic_events_today()

            # Verify the Finnhub call used KST date
            call_args = mock_fetch.call_args
            assert call_args is not None
            from_date = call_args[0][0]
            to_date = call_args[0][1]
            # Should query KST date = "2026-03-19"
            assert from_date == "2026-03-19"
            assert to_date == "2026-03-19"
