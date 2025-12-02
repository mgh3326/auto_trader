"""
Tests for manual holdings integration in KIS automation tasks.

수동 잔고(토스 등)를 자동화 태스크에 통합하는 기능 테스트
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_manual_holding():
    """Create a mock ManualHolding object"""
    holding = MagicMock()
    holding.ticker = "005930"
    holding.display_name = "삼성전자"
    holding.quantity = Decimal("10")
    holding.average_price = Decimal("70000")
    holding.avg_price = Decimal("70000")  # alias
    return holding


class TestManualHoldingsIntegration:
    """수동 잔고 통합 테스트"""

    def test_manual_holdings_merged_with_kis_holdings(self, monkeypatch):
        """수동 잔고가 KIS 잔고와 병합되어 처리되는지 확인"""
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                # KIS에는 삼성전자우만 있음
                return [
                    {
                        "pdno": "005935",
                        "prdt_name": "삼성전자우",
                        "pchs_avg_pric": "73800",
                        "prpr": "75850",
                        "hldg_qty": "5",
                    }
                ]

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

            async def fetch_fundamental_info(self, code):
                # 수동 잔고 종목의 현재가 조회
                if code == "005930":
                    return {"종목명": "삼성전자", "현재가": 71000}
                return {"종목명": "Unknown", "현재가": 0}

        # Mock manual holding (토스에만 있는 삼성전자)
        manual_holding = MagicMock()
        manual_holding.ticker = "005930"
        manual_holding.display_name = "삼성전자"
        manual_holding.quantity = Decimal("10")
        manual_holding.avg_price = Decimal("70000")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                # 토스에 삼성전자가 있음
                return [manual_holding]

        buy_calls = []
        sell_calls = []

        async def fake_buy(kis, symbol, current_price, avg_price):
            buy_calls.append({"symbol": symbol})
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_calls.append({"symbol": symbol, "qty": qty})
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.manual_holdings_service.ManualHoldingsService', MockManualService):

            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell)
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        # 태스크가 성공적으로 완료되어야 함
        assert result["status"] == "completed"
        assert len(result["results"]) == 2, "KIS 1개 + 수동 잔고 1개 = 총 2개"

        # 결과에 두 종목이 모두 포함되어야 함
        codes = [r["code"] for r in result["results"]]
        assert "005935" in codes, "KIS 보유 종목(삼성전자우)이 포함되어야 함"
        assert "005930" in codes, "수동 잔고 종목(삼성전자)이 포함되어야 함"

        # 매수/매도 함수가 두 종목 모두에 대해 호출되어야 함
        assert len(buy_calls) == 2, "두 종목 모두 매수 검토"
        assert len(sell_calls) == 2, "두 종목 모두 매도 검토"

    def test_manual_holdings_duplicates_skipped(self, monkeypatch):
        """KIS와 수동 잔고에 동일 종목이 있으면 수동 잔고는 스킵"""
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                # KIS에 삼성전자가 있음
                return [
                    {
                        "pdno": "005930",
                        "prdt_name": "삼성전자",
                        "pchs_avg_pric": "70000",
                        "prpr": "71000",
                        "hldg_qty": "20",
                    }
                ]

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        # Mock manual holding (토스에도 삼성전자 - 중복)
        manual_holding = MagicMock()
        manual_holding.ticker = "005930"
        manual_holding.display_name = "삼성전자"
        manual_holding.quantity = Decimal("10")
        manual_holding.avg_price = Decimal("69000")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [manual_holding]

        buy_calls = []
        sell_calls = []

        async def fake_buy(kis, symbol, current_price, avg_price):
            buy_calls.append({"symbol": symbol})
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_calls.append({"symbol": symbol})
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.manual_holdings_service.ManualHoldingsService', MockManualService):

            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell)
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        # 태스크가 성공적으로 완료되어야 함
        assert result["status"] == "completed"
        assert len(result["results"]) == 1, "중복은 제거되고 1개만 처리"

        # KIS 종목만 처리되어야 함 (수동 잔고는 스킵)
        assert result["results"][0]["code"] == "005930"

        # 매수/매도 함수는 한 번만 호출
        assert len(buy_calls) == 1
        assert len(sell_calls) == 1

    def test_manual_holdings_current_price_fetched(self, monkeypatch):
        """수동 잔고 종목의 현재가가 API로 조회되는지 확인"""
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        price_fetch_calls = []

        class DummyKIS:
            async def fetch_my_stocks(self):
                return []  # KIS에는 보유 종목 없음

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

            async def fetch_fundamental_info(self, code):
                # 현재가 조회 기록
                price_fetch_calls.append(code)
                return {"종목명": "삼성전자", "현재가": 72000}

        # Mock manual holding
        manual_holding = MagicMock()
        manual_holding.ticker = "005930"
        manual_holding.display_name = "삼성전자"
        manual_holding.quantity = Decimal("10")
        manual_holding.avg_price = Decimal("70000")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [manual_holding]

        async def fake_buy(kis, symbol, current_price, avg_price):
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.manual_holdings_service.ManualHoldingsService', MockManualService):

            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)
            monkeypatch.setattr(kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell)
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        # 태스크가 성공적으로 완료되어야 함
        assert result["status"] == "completed"

        # 현재가 조회 API가 호출되어야 함
        assert "005930" in price_fetch_calls, "수동 잔고 종목의 현재가를 조회해야 함"
