"""Tests for KIS-related Celery tasks."""
import pytest
from typing import Any, Dict, List


def test_run_per_domestic_stock_automation_executes_all_steps(monkeypatch):
    """분석 -> 매수 -> 매도 모든 단계가 실행되고 결과에 포함되는지 확인."""
    from app.tasks import kis as kis_tasks

    class DummyAnalyzer:
        async def analyze_stock_json(self, name):
            return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

        async def close(self):
            return None

    class DummyKIS:
        def __init__(self):
            self.fetch_calls = 0

        async def fetch_my_stocks(self):
            self.fetch_calls += 1
            return [
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                    "hldg_qty": "5",
                }
            ]

    buy_calls = []
    sell_calls = []

    async def fake_buy(kis, symbol, current_price, avg_price):
        buy_calls.append({
            "symbol": symbol,
            "current_price": current_price,
            "avg_price": avg_price,
        })
        return {"success": False, "message": "종목 설정 없음 - 매수 건너뜀", "orders_placed": 0}

    async def fake_sell(kis, symbol, current_price, avg_price, qty):
        sell_calls.append({
            "symbol": symbol,
            "current_price": current_price,
            "avg_price": avg_price,
            "qty": qty,
        })
        return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

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
    assert len(result["results"]) == 1

    stock_result = result["results"][0]
    assert stock_result["name"] == "삼성전자우"
    assert stock_result["code"] == "005935"

    # 모든 단계(분석, 매수, 매도)가 실행되어야 함
    steps = stock_result["steps"]
    assert len(steps) == 3, f"3개의 단계가 있어야 함, 실제: {len(steps)}"

    step_names = [s["step"] for s in steps]
    assert step_names == ["분석", "매수", "매도"], f"단계 순서가 잘못됨: {step_names}"

    # 매수 함수가 호출되어야 함
    assert len(buy_calls) == 1, "매수 함수가 호출되어야 합니다"
    assert buy_calls[0]["symbol"] == "005935"

    # 매도 함수가 호출되어야 함
    assert len(sell_calls) == 1, "매도 함수가 호출되어야 합니다"
    assert sell_calls[0]["symbol"] == "005935"
    assert sell_calls[0]["qty"] == 5


def test_run_per_domestic_stock_automation_with_real_trading_service(monkeypatch):
    """실제 trading service 함수를 사용해서 전체 플로우 테스트 (삼성전자우 시나리오)."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.tasks import kis as kis_tasks
    from app.models.analysis import StockAnalysisResult

    class DummyAnalyzer:
        async def analyze_stock_json(self, name):
            return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

        async def close(self):
            return None

    class DummyKIS:
        def __init__(self):
            self.fetch_calls = 0
            self.order_calls = []

        async def fetch_my_stocks(self):
            self.fetch_calls += 1
            return [
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                    "hldg_qty": "5",
                }
            ]

        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            self.order_calls.append({
                "stock_code": stock_code,
                "order_type": order_type,
                "quantity": quantity,
                "price": price,
            })
            return {"rt_cd": "0", "msg1": "정상처리"}

    dummy_kis = DummyKIS()

    # 실제 분석 결과 mock
    analysis = StockAnalysisResult(
        decision="hold",
        confidence=65,
        appropriate_buy_min=73000,
        appropriate_buy_max=75000,
        appropriate_sell_min=77500,
        appropriate_sell_max=79000,
        buy_hope_min=71500,
        buy_hope_max=72500,
        sell_target_min=85000,
        sell_target_max=87000,
        model_name="gemini-2.5-pro",
        prompt="test prompt"
    )

    # 종목 설정 mock
    mock_settings = MagicMock()
    mock_settings.is_active = True
    mock_settings.buy_price_levels = 1
    mock_settings.buy_quantity_per_order = 1

    monkeypatch.setattr(kis_tasks, "KISClient", lambda: dummy_kis)
    monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
    monkeypatch.setattr(
        kis_tasks.run_per_domestic_stock_automation,
        "update_state",
        lambda *_, **__: None,
        raising=False,
    )

    with patch('app.core.db.AsyncSessionLocal') as mock_session_cls, \
         patch('app.services.stock_info_service.StockAnalysisService') as mock_service_cls, \
         patch('app.services.symbol_trade_settings_service.SymbolTradeSettingsService') as mock_settings_service_cls:

        mock_session_instance = MagicMock()
        mock_session_instance.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_instance.__aexit__ = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session_instance

        mock_service = AsyncMock()
        mock_service_cls.return_value = mock_service
        mock_service.get_latest_analysis_by_symbol.return_value = analysis

        mock_settings_service = AsyncMock()
        mock_settings_service_cls.return_value = mock_settings_service
        mock_settings_service.get_by_symbol.return_value = mock_settings

        result = kis_tasks.run_per_domestic_stock_automation.apply().result

    # 결과 검증
    assert result["status"] == "completed"
    assert len(result["results"]) == 1

    stock_result = result["results"][0]
    steps = stock_result["steps"]

    # 3단계 모두 있어야 함
    assert len(steps) == 3, f"Expected 3 steps, got {len(steps)}: {steps}"

    step_names = [s["step"] for s in steps]
    assert step_names == ["분석", "매수", "매도"], f"단계: {step_names}"

    # 매수 결과 확인 (현재가 > 평단*0.99 이므로 조건 미충족)
    buy_step = next(s for s in steps if s["step"] == "매수")
    assert "1% 매수 조건 미충족" in buy_step["result"]["message"]

    # 매도 결과 확인 - 매도 주문이 실행되어야 함
    sell_step = next(s for s in steps if s["step"] == "매도")
    # 매도 가격: 77500, 79000, 85000, 87000 중 모두 >= min_sell(74538) and >= current(75850)
    # 따라서 4건 분할 매도
    assert sell_step["result"]["success"] is True, f"매도 실패: {sell_step['result']}"
    assert sell_step["result"]["orders_placed"] == 4

    # KIS 주문 호출 확인
    sell_orders = [o for o in dummy_kis.order_calls if o["order_type"] == "sell"]
    assert len(sell_orders) == 4, f"Expected 4 sell orders, got {len(sell_orders)}"


def test_run_per_domestic_stock_automation_handles_buy_exception(monkeypatch):
    """매수 단계에서 예외 발생 시에도 매도 단계가 실행되어야 함."""
    from app.tasks import kis as kis_tasks

    class DummyAnalyzer:
        async def analyze_stock_json(self, name):
            return {"decision": "buy"}, "gemini-2.5-pro"

        async def close(self):
            return None

    class DummyKIS:
        async def fetch_my_stocks(self):
            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "pchs_avg_pric": "50000",
                    "prpr": "51000",
                    "hldg_qty": "10",
                }
            ]

    sell_calls = []

    async def fake_buy(*_, **__):
        raise Exception("DB connection error")

    async def fake_sell(kis, symbol, current_price, avg_price, qty):
        sell_calls.append({"symbol": symbol, "qty": qty})
        return {"success": True, "message": "매도 완료", "orders_placed": 1}

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

    assert result["status"] == "completed"
    steps = result["results"][0]["steps"]

    # 분석, 매수, 매도 3단계 모두 존재해야 함
    assert len(steps) == 3

    # 매수 단계는 실패해야 함
    buy_step = next(s for s in steps if s["step"] == "매수")
    assert buy_step["result"]["success"] is False
    assert "DB connection error" in buy_step["result"]["error"]

    # 매도 단계는 실행되어야 함
    assert len(sell_calls) == 1, "매수 예외에도 불구하고 매도가 실행되어야 함"
    sell_step = next(s for s in steps if s["step"] == "매도")
    assert sell_step["result"]["success"] is True


def test_run_per_domestic_stock_automation_handles_sell_exception(monkeypatch):
    """매도 단계에서 예외 발생 시 결과에 에러가 포함되어야 함."""
    from app.tasks import kis as kis_tasks

    class DummyAnalyzer:
        async def analyze_stock_json(self, name):
            return {"decision": "sell"}, "gemini-2.5-pro"

        async def close(self):
            return None

    class DummyKIS:
        async def fetch_my_stocks(self):
            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "pchs_avg_pric": "50000",
                    "prpr": "60000",
                    "hldg_qty": "10",
                }
            ]

    async def fake_buy(*_, **__):
        return {"success": True, "message": "매수 완료"}

    async def fake_sell(*_, **__):
        raise Exception("Event loop is closed")

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

    assert result["status"] == "completed"
    steps = result["results"][0]["steps"]

    assert len(steps) == 3

    sell_step = next(s for s in steps if s["step"] == "매도")
    assert sell_step["result"]["success"] is False
    assert "Event loop is closed" in sell_step["result"]["error"]


def test_run_per_domestic_stock_automation_refreshes_holdings(monkeypatch):
    """매수 이후 잔고를 다시 불러와 최신 수량으로 매도하는지 확인."""
    from app.tasks import kis as kis_tasks

    class DummyAnalyzer:
        async def analyze_stock_json(self, *_):
            return {"status": "ok"}

        async def close(self):
            return None

    class DummyKIS:
        def __init__(self):
            self.fetch_calls: int = 0

        async def fetch_my_stocks(self) -> List[Dict[str, Any]]:
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return [
                    {
                        "pdno": "005930",
                        "prdt_name": "삼성전자",
                        "pchs_avg_pric": "50000",
                        "prpr": "51000",
                        "hldg_qty": "10",
                    }
                ]

            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "pchs_avg_pric": "50500",
                    "prpr": "51500",
                    "hldg_qty": "12",
                }
            ]

    sell_calls: List[Dict[str, Any]] = []

    async def fake_buy(*_, **__):
        return {"success": True}

    async def fake_sell(_kis, symbol, current_price, avg_price, qty):
        sell_calls.append(
            {
                "symbol": symbol,
                "current_price": current_price,
                "avg_price": avg_price,
                "qty": qty,
            }
        )
        return {"success": True}

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

    assert result["status"] == "completed"
    assert sell_calls, "매도 단계가 호출되어야 합니다."
    # 매수 이후 최신 잔고(12주)와 갱신된 가격을 사용해야 한다.
    assert sell_calls[0]["qty"] == 12
    assert sell_calls[0]["avg_price"] == 50500.0
    assert sell_calls[0]["current_price"] == 51500.0


def test_execute_overseas_buy_order_fetches_price_for_new_symbol(monkeypatch):
    """보유하지 않은 해외 주식 매수 시 현재가를 조회해 주문 파라미터에 전달한다."""
    from app.tasks import kis as kis_tasks

    class DummyKIS:
        async def fetch_my_overseas_stocks(self):
            return []

        async def fetch_overseas_price(self, symbol, exchange_code="NASD"):
            assert symbol == "AAPL"
            assert exchange_code == "NASD"
            return 123.45

    captured: Dict[str, Any] = {}

    async def fake_process(_kis, symbol, current_price, avg_price):
        captured.update(
            {
                "symbol": symbol,
                "current_price": current_price,
                "avg_price": avg_price,
            }
        )
        return {"success": True}

    monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
    monkeypatch.setattr(kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_process)

    result = kis_tasks.execute_overseas_buy_order_task.apply(args=("AAPL",)).result

    assert result["success"] is True
    assert captured["symbol"] == "AAPL"
    assert captured["avg_price"] == 0.0  # 신규 매수이므로 평단가는 0으로 전달
    assert captured["current_price"] == 123.45


class TestStepErrorReporting:
    """태스크 step 에러 알림 테스트."""

    def test_report_step_error_async_not_enabled(self, monkeypatch):
        """ErrorReporter가 비활성화 상태일 때 알림을 보내지 않음."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        from app.tasks import kis as kis_tasks

        mock_reporter = MagicMock()
        mock_reporter._enabled = False
        mock_reporter.send_error_to_telegram = AsyncMock()

        monkeypatch.setattr(kis_tasks, "get_error_reporter", lambda: mock_reporter)

        # 함수 실행
        asyncio.run(kis_tasks._report_step_error_async(
            "test_task", "삼성전자", "005930", "매도", "Test error"
        ))

        # send_error_to_telegram이 호출되지 않아야 함
        mock_reporter.send_error_to_telegram.assert_not_called()

    def test_report_step_error_async_sends_telegram_when_enabled(self, monkeypatch):
        """ErrorReporter가 활성화 상태일 때 Telegram 알림 전송."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        from app.tasks import kis as kis_tasks

        mock_reporter = MagicMock()
        mock_reporter._enabled = True
        mock_reporter.send_error_to_telegram = AsyncMock(return_value=True)

        monkeypatch.setattr(kis_tasks, "get_error_reporter", lambda: mock_reporter)

        # 함수 실행
        asyncio.run(kis_tasks._report_step_error_async(
            "kis.run_per_domestic_stock_automation",
            "삼성전자우",
            "005935",
            "매도",
            "unexpected keyword argument 'symbol'"
        ))

        # send_error_to_telegram이 호출되어야 함
        mock_reporter.send_error_to_telegram.assert_called_once()

        # 호출 인자 확인
        call_args = mock_reporter.send_error_to_telegram.call_args
        assert "unexpected keyword argument" in str(call_args.kwargs["error"])
        assert call_args.kwargs["additional_context"]["task_name"] == "kis.run_per_domestic_stock_automation"
        assert call_args.kwargs["additional_context"]["stock"] == "삼성전자우 (005935)"
        assert call_args.kwargs["additional_context"]["step"] == "매도"

    def test_automation_task_reports_error_on_exception(self, monkeypatch):
        """태스크에서 예외 발생 시 알림 함수가 호출되는지 확인."""
        from unittest.mock import AsyncMock, MagicMock
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold"}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                return [
                    {
                        "pdno": "005935",
                        "prdt_name": "삼성전자우",
                        "pchs_avg_pric": "73800",
                        "prpr": "75850",
                        "hldg_qty": "5",
                    }
                ]

        error_reports = []

        async def fake_report_error(task_name, stock_name, stock_code, step_name, error_msg):
            error_reports.append({
                "task_name": task_name,
                "stock_name": stock_name,
                "stock_code": stock_code,
                "step_name": step_name,
                "error_msg": error_msg,
            })

        async def fake_buy(*_, **__):
            return {"success": True, "message": "매수 완료"}

        async def fake_sell(*_, **__):
            raise TypeError("unexpected keyword argument 'symbol'")

        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)
        monkeypatch.setattr(kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell)
        monkeypatch.setattr(kis_tasks, "_report_step_error_async", fake_report_error)
        monkeypatch.setattr(
            kis_tasks.run_per_domestic_stock_automation,
            "update_state",
            lambda *_, **__: None,
            raising=False,
        )

        result = kis_tasks.run_per_domestic_stock_automation.apply().result

        # 태스크는 완료되어야 함 (에러를 catch하므로)
        assert result["status"] == "completed"

        # 에러 알림이 전송되어야 함
        assert len(error_reports) == 1, f"Expected 1 error report, got {len(error_reports)}"
        assert error_reports[0]["task_name"] == "kis.run_per_domestic_stock_automation"
        assert error_reports[0]["stock_name"] == "삼성전자우"
        assert error_reports[0]["stock_code"] == "005935"
        assert error_reports[0]["step_name"] == "매도"
        assert "unexpected keyword argument" in error_reports[0]["error_msg"]

    def test_automation_task_reports_error_from_result(self, monkeypatch):
        """결과에 error 필드가 있을 때 알림이 전송되는지 확인."""
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold"}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_stocks(self):
                return [
                    {
                        "pdno": "005935",
                        "prdt_name": "삼성전자우",
                        "pchs_avg_pric": "73800",
                        "prpr": "75850",
                        "hldg_qty": "5",
                    }
                ]

        error_reports = []

        async def fake_report_error(task_name, stock_name, stock_code, step_name, error_msg):
            error_reports.append({
                "task_name": task_name,
                "step_name": step_name,
                "error_msg": error_msg,
            })

        async def fake_buy(*_, **__):
            # 예외가 아닌 결과에 error 포함
            return {"success": False, "error": "DB connection failed", "orders_placed": 0}

        async def fake_sell(*_, **__):
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)
        monkeypatch.setattr(kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell)
        monkeypatch.setattr(kis_tasks, "_report_step_error_async", fake_report_error)
        monkeypatch.setattr(
            kis_tasks.run_per_domestic_stock_automation,
            "update_state",
            lambda *_, **__: None,
            raising=False,
        )

        result = kis_tasks.run_per_domestic_stock_automation.apply().result

        assert result["status"] == "completed"

        # 매수 결과의 error 필드로 인해 알림이 전송되어야 함
        assert len(error_reports) == 1
        assert error_reports[0]["step_name"] == "매수"
        assert "DB connection failed" in error_reports[0]["error_msg"]
