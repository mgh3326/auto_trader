"""
Tests for API routers.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import api


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(api)


class TestHealthRouter:
    """Test health check endpoints."""

    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] == "ok"


class TestDashboardRouter:
    """Test dashboard endpoints."""

    def test_get_dashboard_data(self, client):
        """Test dashboard data endpoint."""
        response = client.get("/dashboard/")
        assert response.status_code == 200
        # Add more specific assertions based on your actual endpoint

    def test_get_analysis_list(self, client):
        """Test analysis list endpoint."""
        response = client.get("/dashboard/analysis")
        assert response.status_code == 200
        # Add more specific assertions based on your actual endpoint


class TestRouterIntegration:
    """Test router integration."""

    def test_router_registration(self, client):
        """Test that all routers are properly registered."""
        # Test that the main app has the expected routers
        app = client.app
        routes = [route.path for route in app.routes]

        # Check that expected routes exist
        assert any("/healthz" in route for route in routes)
        assert any("/dashboard" in route for route in routes)
        assert any("/analysis" in route for route in routes)
        assert any("/screener" in route for route in routes)


class TestUpbitTradingRouter:
    """Test Upbit trading router behaviours."""

    @pytest.mark.asyncio
    async def test_get_my_coins_raises_http_exception_on_failure(self, monkeypatch):
        """get_my_coins는 내부 오류 시 HTTPException을 일관되게 전달해야 한다."""
        from app.routers import upbit_trading

        async def fake_prime():
            return None

        async def fake_fetch_my_coins():
            raise RuntimeError("boom")

        class DummyAnalyzer:
            def _is_tradable(self, coin):
                return True

            def is_tradable(self, coin):
                return self._is_tradable(coin)

            async def close(self):
                return None

        monkeypatch.setattr(
            "app.services.upbit_symbol_universe_service.prime_upbit_constants",
            fake_prime,
        )
        monkeypatch.setattr(
            "app.services.upbit.fetch_my_coins",
            fake_fetch_my_coins,
        )
        monkeypatch.setattr(
            upbit_trading,
            "UpbitAnalyzer",
            DummyAnalyzer,
        )

        with pytest.raises(HTTPException) as exc_info:
            await upbit_trading.get_my_coins(db=object())

        assert exc_info.value.status_code == 500
        assert "boom" in exc_info.value.detail


class TestKISOverseasTradingRouter:
    @pytest.mark.asyncio
    async def test_get_my_overseas_stocks_usd_row_missing_returns_500(
        self, monkeypatch
    ):
        from app.routers import kis_overseas_trading

        class FakeKISClient:
            async def inquire_overseas_margin(self):
                return []

            async def inquire_integrated_margin(self):
                raise AssertionError("inquire_integrated_margin should not be called")

        class FakeMergedPortfolioService:
            def __init__(self, db):
                self.db = db

            async def get_merged_portfolio_overseas(self, user_id, kis):
                return []

        monkeypatch.setattr(kis_overseas_trading, "KISClient", FakeKISClient)
        monkeypatch.setattr(
            kis_overseas_trading,
            "MergedPortfolioService",
            FakeMergedPortfolioService,
        )

        with pytest.raises(HTTPException) as exc_info:
            await kis_overseas_trading.get_my_overseas_stocks(
                db=AsyncMock(), current_user=MagicMock(id=1)
            )

        assert exc_info.value.status_code == 500
        assert "USD margin data not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_my_overseas_stocks_does_not_call_integrated_margin(
        self, monkeypatch
    ):
        from app.routers import kis_overseas_trading

        call_state = {"integrated_called": False}

        class FakeKISClient:
            async def inquire_overseas_margin(self):
                return [
                    {
                        "natn_name": "미국",
                        "crcy_cd": "USD",
                        "frcr_dncl_amt1": "321.5",
                        "frcr_gnrl_ord_psbl_amt": "300.0",
                    }
                ]

            async def inquire_integrated_margin(self):
                call_state["integrated_called"] = True
                raise RuntimeError("inquire_integrated_margin should not be called")

        class FakeMergedPortfolioService:
            def __init__(self, db):
                self.db = db

            async def get_merged_portfolio_overseas(self, user_id, kis):
                return []

        monkeypatch.setattr(kis_overseas_trading, "KISClient", FakeKISClient)
        monkeypatch.setattr(
            kis_overseas_trading,
            "MergedPortfolioService",
            FakeMergedPortfolioService,
        )

        result = await kis_overseas_trading.get_my_overseas_stocks(
            db=AsyncMock(), current_user=MagicMock(id=1)
        )

        assert result["success"] is True
        assert result["usd_balance"] == 321.5
        assert call_state["integrated_called"] is False


class TestManualHoldingsRouter:
    @pytest.mark.asyncio
    async def test_create_holding_maps_validation_error_to_400(self, monkeypatch):
        from app.models.manual_holdings import BrokerType, MarketType
        from app.routers import manual_holdings
        from app.schemas.manual_holdings import ManualHoldingCreate
        from app.services.manual_holdings_service import ManualHoldingValidationError

        account_service = AsyncMock()
        account_service.get_or_create_default_account.return_value = SimpleNamespace(
            id=1,
            broker_type="toss",
            account_name="기본 계좌",
        )
        holdings_service = AsyncMock()
        holdings_service.upsert_holding.side_effect = ManualHoldingValidationError(
            "invalid US ticker"
        )

        monkeypatch.setattr(
            manual_holdings,
            "BrokerAccountService",
            lambda db: account_service,
        )
        monkeypatch.setattr(
            manual_holdings,
            "ManualHoldingsService",
            lambda db: holdings_service,
        )

        data = ManualHoldingCreate(
            broker_type=BrokerType.TOSS,
            account_name="기본 계좌",
            ticker="솔라나",
            market_type=MarketType.US,
            quantity=1,
            avg_price=10,
            display_name="솔라나",
        )

        with pytest.raises(HTTPException) as exc_info:
            await manual_holdings.create_holding(
                data=data,
                current_user=SimpleNamespace(id=1),
                db=AsyncMock(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid US ticker"

    @pytest.mark.asyncio
    async def test_create_holdings_bulk_maps_validation_error_to_400(self, monkeypatch):
        from app.models.manual_holdings import BrokerType, MarketType
        from app.routers import manual_holdings
        from app.schemas.manual_holdings import ManualHoldingBulkCreate
        from app.services.manual_holdings_service import ManualHoldingValidationError

        account_service = AsyncMock()
        account_service.get_account_by_user_and_broker.return_value = SimpleNamespace(
            id=1,
            broker_type="toss",
            account_name="기본 계좌",
        )

        holdings_service = AsyncMock()
        holdings_service.bulk_create_holdings.side_effect = (
            ManualHoldingValidationError("invalid mixed holdings")
        )

        monkeypatch.setattr(
            manual_holdings,
            "BrokerAccountService",
            lambda db: account_service,
        )
        monkeypatch.setattr(
            manual_holdings,
            "ManualHoldingsService",
            lambda db: holdings_service,
        )

        data = ManualHoldingBulkCreate(
            broker_type=BrokerType.TOSS,
            account_name="기본 계좌",
            holdings=[
                {
                    "ticker": "AAPL",
                    "market_type": MarketType.US,
                    "quantity": 1,
                    "avg_price": 100,
                },
                {
                    "ticker": "솔라나",
                    "market_type": MarketType.US,
                    "quantity": 1,
                    "avg_price": 100,
                },
            ],
        )

        with pytest.raises(HTTPException) as exc_info:
            await manual_holdings.create_holdings_bulk(
                data=data,
                current_user=SimpleNamespace(id=1),
                db=AsyncMock(),
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "invalid mixed holdings"
