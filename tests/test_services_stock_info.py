from decimal import Decimal

import pytest

from app.services import stock_info_service


class TestStockInfoServiceGuard:
    @pytest.mark.asyncio
    async def test_process_buy_orders_enforces_one_percent_guard(self, monkeypatch):
        async def fake_check(required_amount):
            return True, Decimal("200000")

        class DummyAsyncSession:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class DummyAnalysis:
            appropriate_buy_min = Decimal("90")
            appropriate_buy_max = Decimal("95")
            buy_hope_min = Decimal("92")
            buy_hope_max = Decimal("94")

        class DummyService:
            def __init__(self, db):
                self.db = db

            async def get_latest_analysis_by_symbol(self, symbol):
                return DummyAnalysis()

        from app.core.config import settings

        monkeypatch.setattr(
            "app.services.brokers.upbit.client.check_krw_balance_sufficient",
            fake_check,
        )
        monkeypatch.setattr(
            "app.core.db.AsyncSessionLocal", lambda: DummyAsyncSession()
        )
        monkeypatch.setattr(
            stock_info_service,
            "StockAnalysisService",
            DummyService,
        )
        monkeypatch.setattr(
            settings, "upbit_min_krw_balance", Decimal("10000"), raising=False
        )
        monkeypatch.setattr(
            settings, "upbit_buy_amount", Decimal("10000"), raising=False
        )

        calls = []

        async def fake_place(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "success": True,
                "message": "should not be returned",
                "orders_placed": 1,
                "total_amount": 10000.0,
            }

        monkeypatch.setattr(
            stock_info_service,
            "_place_multiple_buy_orders_by_analysis",
            fake_place,
        )

        result = await stock_info_service.process_buy_orders_with_analysis(
            symbol="KRW-ABC",
            current_price=100.0,
            avg_buy_price=100.0,
        )

        assert result["success"] is False
        assert "목표가" in result["message"]
        assert calls == []
