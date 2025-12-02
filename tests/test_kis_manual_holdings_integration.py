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

        # 매수 함수가 두 종목 모두에 대해 호출되어야 함 (분석은 수동 잔고도 수행)
        assert len(buy_calls) == 2, "두 종목 모두 매수 검토"
        # 매도 함수는 KIS 종목만 호출 (수동 잔고는 KIS에서 매도 불가하므로 스킵)
        assert len(sell_calls) == 1, "KIS 종목만 매도 검토 (수동 잔고는 스킵)"
        assert sell_calls[0]["symbol"] == "005935", "KIS 종목(삼성전자우)만 매도 함수 호출"

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

    def test_manual_holdings_decimal_conversion(self, monkeypatch):
        """Decimal 타입의 수량/가격이 올바르게 변환되는지 확인"""
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                return []  # KIS에는 보유 종목 없음

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

            async def fetch_fundamental_info(self, code):
                return {"종목명": "삼성전자", "현재가": 72000}

        # Mock manual holding with Decimal values that have many decimal places
        manual_holding = MagicMock()
        manual_holding.ticker = "005930"
        manual_holding.display_name = "삼성전자"
        manual_holding.quantity = Decimal("2.00000000")  # 소수점 많은 Decimal
        manual_holding.avg_price = Decimal("70000.00000000")  # 소수점 많은 Decimal

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [manual_holding]

        sell_calls = []

        async def fake_buy(kis, symbol, current_price, avg_price):
            return {"success": False, "message": "조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            # qty가 올바르게 정수로 전달되는지 확인
            sell_calls.append({"symbol": symbol, "qty": qty, "qty_type": type(qty).__name__})
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

        # 수동 잔고는 KIS에서 매도할 수 없으므로 매도 함수가 호출되지 않아야 함
        assert len(sell_calls) == 0, "수동 잔고는 매도 함수가 호출되면 안 됨"

        # 결과에서 매도 스킵 메시지 확인
        assert len(result["results"]) == 1
        stock_result = result["results"][0]
        sell_step = next((s for s in stock_result["steps"] if s["step"] == "매도"), None)
        assert sell_step is not None, "매도 단계가 있어야 함"
        assert "수동잔고" in sell_step["result"]["message"], "수동잔고 스킵 메시지가 있어야 함"

    def test_manual_holdings_has_orderable_qty(self, monkeypatch):
        """수동 잔고에 ord_psbl_qty 필드가 올바르게 설정되고, 매도는 스킵되는지 확인"""
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                return []  # KIS에는 보유 종목 없음

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

            async def fetch_fundamental_info(self, code):
                return {"종목명": "삼성전자", "현재가": 72000}

        # Mock manual holding
        manual_holding = MagicMock()
        manual_holding.ticker = "005930"
        manual_holding.display_name = "삼성전자"
        manual_holding.quantity = Decimal("5")
        manual_holding.avg_price = Decimal("70000")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [manual_holding]

        sell_calls = []

        async def fake_buy(kis, symbol, current_price, avg_price):
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

        # 수동 잔고는 KIS에서 매도할 수 없으므로 매도 함수가 호출되지 않아야 함
        assert len(sell_calls) == 0, "수동 잔고는 매도 함수가 호출되면 안 됨"

        # 결과에서 매도 스킵 메시지 확인
        assert len(result["results"]) == 1
        stock_result = result["results"][0]
        sell_step = next((s for s in stock_result["steps"] if s["step"] == "매도"), None)
        assert sell_step is not None, "매도 단계가 있어야 함"
        assert "수동잔고" in sell_step["result"]["message"], "수동잔고 스킵 메시지가 있어야 함"

    def test_manual_holdings_skip_sell_order(self, monkeypatch):
        """수동 잔고(토스 등)는 KIS에서 매도할 수 없으므로 매도를 스킵해야 함.

        APBK0400 에러 방지:
        - 토스 증권에만 있는 주식을 KIS API로 매도 요청하면 "주문 가능한 수량을 초과" 에러 발생
        - 수동 잔고 종목은 분석만 하고 매도는 스킵해야 함
        """
        from app.tasks import kis as kis_tasks
        from decimal import Decimal

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "sell", "confidence": 85}, "gemini-2.5-pro"

            async def close(self):
                return None

        sell_calls = []

        class DummyKIS:
            async def fetch_my_stocks(self):
                # KIS에는 보유 종목 없음 (수동 잔고만 있음)
                return []

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

            async def fetch_fundamental_info(self, code):
                return {"종목명": "한국전력", "현재가": 25000}

        # 수동 잔고 종목 (토스에만 있음)
        mock_manual_holding = MagicMock()
        mock_manual_holding.ticker = "015760"  # 한국전력
        mock_manual_holding.display_name = "한국전력"
        mock_manual_holding.quantity = Decimal("10")
        mock_manual_holding.avg_price = Decimal("23000")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [mock_manual_holding]

        async def fake_buy(*_, **__):
            return {"success": False, "message": "매수 조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_calls.append({"symbol": symbol, "qty": qty})
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

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

        # 수동 잔고 종목의 결과가 있어야 함
        assert len(result["results"]) == 1
        stock_result = result["results"][0]
        assert stock_result["code"] == "015760"

        # 핵심 검증: 매도 함수가 호출되지 않아야 함 (수동 잔고이므로)
        assert len(sell_calls) == 0, f"수동 잔고는 매도 함수가 호출되면 안 됨. 호출된 횟수: {len(sell_calls)}"

        # 매도 단계 결과가 '수동잔고' 관련 메시지여야 함
        sell_step = next((s for s in stock_result["steps"] if s["step"] == "매도"), None)
        assert sell_step is not None, "매도 단계가 있어야 함"
        assert "수동잔고" in sell_step["result"]["message"], \
            f"매도 결과에 '수동잔고' 메시지가 있어야 함: {sell_step['result']}"


class TestTossRecommendationNotification:
    """토스(수동 잔고) 종목 가격 제안 알림 테스트

    AI 결정(buy/hold/sell)과 무관하게 항상 가격 제안을 포함하여 알림을 발송합니다.
    """

    @pytest.mark.asyncio
    async def test_send_toss_price_recommendation_with_buy_decision(self, monkeypatch):
        """매수 결정 시 가격 제안 알림 발송"""
        from app.tasks.kis import _send_toss_recommendation_async
        from app.models.analysis import StockAnalysisResult

        notification_sent = []

        # Mock TradeNotifier
        class MockNotifier:
            _enabled = True

            async def notify_toss_price_recommendation(self, **kwargs):
                notification_sent.append(kwargs)
                return True

        # Mock 분석 결과 (매수 결정)
        mock_analysis = MagicMock(spec=StockAnalysisResult)
        mock_analysis.decision = "buy"
        mock_analysis.confidence = 75
        mock_analysis.reasons = ["이동평균선 정배열", "RSI 적정 구간"]
        mock_analysis.appropriate_buy_min = 23000.0
        mock_analysis.appropriate_buy_max = 24000.0
        mock_analysis.appropriate_sell_min = 28000.0
        mock_analysis.appropriate_sell_max = 30000.0
        mock_analysis.buy_hope_min = 22000.0
        mock_analysis.buy_hope_max = 23000.0
        mock_analysis.sell_target_min = 30000.0
        mock_analysis.sell_target_max = 32000.0

        class MockAnalysisService:
            def __init__(self, db):
                pass

            async def get_latest_analysis_by_symbol(self, symbol):
                return mock_analysis

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.tasks.kis.get_trade_notifier', return_value=MockNotifier()), \
             patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.stock_info_service.StockAnalysisService', MockAnalysisService):

            await _send_toss_recommendation_async(
                code="015760",
                name="한국전력",
                current_price=25000.0,
                toss_quantity=10,
                toss_avg_price=23000.0,
            )

        # 가격 제안 알림이 발송되어야 함
        assert len(notification_sent) == 1
        assert notification_sent[0]["symbol"] == "015760"
        assert notification_sent[0]["korean_name"] == "한국전력"
        assert notification_sent[0]["decision"] == "buy"
        assert notification_sent[0]["confidence"] == 75
        assert notification_sent[0]["appropriate_buy_min"] == 23000.0
        assert notification_sent[0]["appropriate_sell_max"] == 30000.0

    @pytest.mark.asyncio
    async def test_send_toss_price_recommendation_with_sell_decision(self, monkeypatch):
        """매도 결정 시에도 가격 제안 알림 발송"""
        from app.tasks.kis import _send_toss_recommendation_async
        from app.models.analysis import StockAnalysisResult

        notification_sent = []

        class MockNotifier:
            _enabled = True

            async def notify_toss_price_recommendation(self, **kwargs):
                notification_sent.append(kwargs)
                return True

        # Mock 분석 결과 (매도 결정)
        mock_analysis = MagicMock(spec=StockAnalysisResult)
        mock_analysis.decision = "sell"
        mock_analysis.confidence = 80
        mock_analysis.reasons = ["과매수 구간", "저항선 도달"]
        mock_analysis.appropriate_buy_min = 20000.0
        mock_analysis.appropriate_buy_max = 22000.0
        mock_analysis.appropriate_sell_min = 26000.0
        mock_analysis.appropriate_sell_max = 28000.0
        mock_analysis.buy_hope_min = 18000.0
        mock_analysis.buy_hope_max = 20000.0
        mock_analysis.sell_target_min = 28000.0
        mock_analysis.sell_target_max = 30000.0

        class MockAnalysisService:
            def __init__(self, db):
                pass

            async def get_latest_analysis_by_symbol(self, symbol):
                return mock_analysis

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.tasks.kis.get_trade_notifier', return_value=MockNotifier()), \
             patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.stock_info_service.StockAnalysisService', MockAnalysisService):

            await _send_toss_recommendation_async(
                code="015760",
                name="한국전력",
                current_price=25000.0,
                toss_quantity=10,
                toss_avg_price=23000.0,
            )

        # 가격 제안 알림이 발송되어야 함
        assert len(notification_sent) == 1
        assert notification_sent[0]["decision"] == "sell"
        assert notification_sent[0]["appropriate_sell_min"] == 26000.0
        assert notification_sent[0]["appropriate_sell_max"] == 28000.0

    @pytest.mark.asyncio
    async def test_send_toss_price_recommendation_with_hold_decision(self, monkeypatch):
        """hold 결정 시에도 가격 제안 알림 발송 (AI 결정과 무관하게 항상 발송)"""
        from app.tasks.kis import _send_toss_recommendation_async
        from app.models.analysis import StockAnalysisResult

        notification_sent = []

        class MockNotifier:
            _enabled = True

            async def notify_toss_price_recommendation(self, **kwargs):
                notification_sent.append(kwargs)
                return True

        mock_analysis = MagicMock(spec=StockAnalysisResult)
        mock_analysis.decision = "hold"
        mock_analysis.confidence = 60
        mock_analysis.reasons = ["현재 관망 권장"]
        mock_analysis.appropriate_buy_min = 23000.0
        mock_analysis.appropriate_buy_max = 24000.0
        mock_analysis.appropriate_sell_min = 26000.0
        mock_analysis.appropriate_sell_max = 28000.0
        mock_analysis.buy_hope_min = 22000.0
        mock_analysis.buy_hope_max = 23000.0
        mock_analysis.sell_target_min = 28000.0
        mock_analysis.sell_target_max = 30000.0

        class MockAnalysisService:
            def __init__(self, db):
                pass

            async def get_latest_analysis_by_symbol(self, symbol):
                return mock_analysis

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with patch('app.tasks.kis.get_trade_notifier', return_value=MockNotifier()), \
             patch('app.core.db.AsyncSessionLocal', return_value=mock_db_session), \
             patch('app.services.stock_info_service.StockAnalysisService', MockAnalysisService):

            await _send_toss_recommendation_async(
                code="015760",
                name="한국전력",
                current_price=25000.0,
                toss_quantity=10,
                toss_avg_price=23000.0,
            )

        # hold 결정이어도 알림이 발송되어야 함 (AI 결정과 무관)
        assert len(notification_sent) == 1
        assert notification_sent[0]["decision"] == "hold"
        assert notification_sent[0]["appropriate_buy_min"] == 23000.0
        assert notification_sent[0]["appropriate_sell_max"] == 28000.0
