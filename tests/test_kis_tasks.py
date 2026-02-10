"""Tests for KIS-related Celery tasks."""

from typing import Any


def test_run_per_domestic_stock_automation_executes_all_steps(monkeypatch):
    """분석 -> 매수 -> 매도 모든 단계가 실행되고 결과에 포함되는지 확인."""
    from unittest.mock import AsyncMock, MagicMock, patch

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

        async def inquire_korea_orders(self, *args, **kwargs):
            return []

        async def cancel_korea_order(self, *args, **kwargs):
            return {"odno": "0000001"}

    # Mock ManualHoldingsService to return empty list
    class MockManualService:
        def __init__(self, db):
            pass

        async def get_holdings_by_user(self, user_id, market_type):
            return []  # No manual holdings

    buy_calls = []
    sell_calls = []

    async def fake_buy(kis, symbol, current_price, avg_price):
        buy_calls.append(
            {
                "symbol": symbol,
                "current_price": current_price,
                "avg_price": avg_price,
            }
        )
        return {
            "success": False,
            "message": "종목 설정 없음 - 매수 건너뜀",
            "orders_placed": 0,
        }

    async def fake_sell(kis, symbol, current_price, avg_price, qty):
        sell_calls.append(
            {
                "symbol": symbol,
                "current_price": current_price,
                "avg_price": avg_price,
                "qty": qty,
            }
        )
        return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

    # Mock DB session
    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_db_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
        patch(
            "app.services.manual_holdings_service.ManualHoldingsService",
            MockManualService,
        ),
    ):
        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
        )
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
        )
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

    # 모든 단계(분석, 매수, 매도)가 실행되어야 함 (미체결 주문이 없으면 취소 단계 생략)
    steps = stock_result["steps"]
    step_names = [s["step"] for s in steps]
    assert "분석" in step_names
    assert "매수" in step_names
    assert "매도" in step_names

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

    from app.models.analysis import StockAnalysisResult
    from app.tasks import kis as kis_tasks

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
            self.order_calls.append(
                {
                    "stock_code": stock_code,
                    "order_type": order_type,
                    "quantity": quantity,
                    "price": price,
                }
            )
            return {"odno": "0001234567", "ord_tmd": "091500", "msg": "정상처리"}

        async def inquire_korea_orders(self, *args, **kwargs):
            return []

        async def cancel_korea_order(self, *args, **kwargs):
            return {"odno": "0000001"}

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
        prompt="test prompt",
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

    # Mock ManualHoldingsService to return empty list
    class MockManualService:
        def __init__(self, db):
            pass

        async def get_holdings_by_user(self, user_id, market_type):
            return []  # No manual holdings

    with (
        patch("app.core.db.AsyncSessionLocal") as mock_session_cls,
        patch(
            "app.services.stock_info_service.StockAnalysisService"
        ) as mock_service_cls,
        patch(
            "app.services.symbol_trade_settings_service.SymbolTradeSettingsService"
        ) as mock_settings_service_cls,
        patch(
            "app.services.manual_holdings_service.ManualHoldingsService",
            MockManualService,
        ),
    ):
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

    # 모든 필수 단계가 있어야 함 (미체결 주문이 없으면 취소 단계 생략)
    step_names = [s["step"] for s in steps]
    assert "분석" in step_names
    assert "매수" in step_names
    assert "매도" in step_names

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
    from unittest.mock import AsyncMock, MagicMock, patch

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

        async def inquire_korea_orders(self, *args, **kwargs):
            return []

        async def cancel_korea_order(self, *args, **kwargs):
            return {"odno": "0000001"}

    # Mock ManualHoldingsService to return empty list
    class MockManualService:
        def __init__(self, db):
            pass

        async def get_holdings_by_user(self, user_id, market_type):
            return []

    sell_calls = []

    async def fake_buy(*_, **__):
        raise Exception("DB connection error")

    async def fake_sell(kis, symbol, current_price, avg_price, qty):
        sell_calls.append({"symbol": symbol, "qty": qty})
        return {"success": True, "message": "매도 완료", "orders_placed": 1}

    # Mock DB session
    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_db_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
        patch(
            "app.services.manual_holdings_service.ManualHoldingsService",
            MockManualService,
        ),
    ):
        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
        )
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
        )
        monkeypatch.setattr(
            kis_tasks.run_per_domestic_stock_automation,
            "update_state",
            lambda *_, **__: None,
            raising=False,
        )

        result = kis_tasks.run_per_domestic_stock_automation.apply().result

    assert result["status"] == "completed"
    steps = result["results"][0]["steps"]
    step_names = [s["step"] for s in steps]

    # 분석, 매수, 매도 필수 단계가 존재해야 함
    assert "분석" in step_names
    assert "매수" in step_names
    assert "매도" in step_names

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
    from unittest.mock import AsyncMock, MagicMock, patch

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

        async def inquire_korea_orders(self, *args, **kwargs):
            return []

        async def cancel_korea_order(self, *args, **kwargs):
            return {"odno": "0000001"}

    # Mock ManualHoldingsService to return empty list
    class MockManualService:
        def __init__(self, db):
            pass

        async def get_holdings_by_user(self, user_id, market_type):
            return []

    async def fake_buy(*_, **__):
        return {"success": True, "message": "매수 완료"}

    async def fake_sell(*_, **__):
        raise Exception("Event loop is closed")

    # Mock DB session
    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_db_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
        patch(
            "app.services.manual_holdings_service.ManualHoldingsService",
            MockManualService,
        ),
    ):
        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
        )
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
        )
        monkeypatch.setattr(
            kis_tasks.run_per_domestic_stock_automation,
            "update_state",
            lambda *_, **__: None,
            raising=False,
        )

        result = kis_tasks.run_per_domestic_stock_automation.apply().result

    assert result["status"] == "completed"
    steps = result["results"][0]["steps"]
    step_names = [s["step"] for s in steps]

    assert "분석" in step_names
    assert "매수" in step_names
    assert "매도" in step_names

    sell_step = next(s for s in steps if s["step"] == "매도")
    assert sell_step["result"]["success"] is False
    assert "Event loop is closed" in sell_step["result"]["error"]


def test_run_per_domestic_stock_automation_refreshes_holdings(monkeypatch):
    """매수 이후 잔고를 다시 불러와 최신 수량으로 매도하는지 확인."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.tasks import kis as kis_tasks

    class DummyAnalyzer:
        async def analyze_stock_json(self, *_):
            return {"status": "ok"}

        async def close(self):
            return None

    class DummyKIS:
        def __init__(self):
            self.fetch_calls: int = 0

        async def fetch_my_stocks(self) -> list[dict[str, Any]]:
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

        async def inquire_korea_orders(self, *args, **kwargs):
            return []

        async def cancel_korea_order(self, *args, **kwargs):
            return {"odno": "0000001"}

    # Mock ManualHoldingsService to return empty list
    class MockManualService:
        def __init__(self, db):
            pass

        async def get_holdings_by_user(self, user_id, market_type):
            return []

    sell_calls: list[dict[str, Any]] = []

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

    # Mock DB session
    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_db_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
        patch(
            "app.services.manual_holdings_service.ManualHoldingsService",
            MockManualService,
        ),
    ):
        monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
        monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
        )
        monkeypatch.setattr(
            kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
        )
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

    captured: dict[str, Any] = {}

    async def fake_process(
        _kis, symbol, current_price, avg_price, exchange_code="NASD"
    ):
        captured.update(
            {
                "symbol": symbol,
                "current_price": current_price,
                "avg_price": avg_price,
            }
        )
        return {"success": True}

    monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
    monkeypatch.setattr(
        kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_process
    )

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
        from unittest.mock import AsyncMock, MagicMock

        from app.tasks import kis as kis_tasks

        mock_reporter = MagicMock()
        mock_reporter._enabled = False
        mock_reporter.send_error_to_telegram = AsyncMock()

        monkeypatch.setattr(kis_tasks, "get_error_reporter", lambda: mock_reporter)

        # 함수 실행
        asyncio.run(
            kis_tasks._report_step_error_async(
                "test_task", "삼성전자", "005930", "매도", "Test error"
            )
        )

        # send_error_to_telegram이 호출되지 않아야 함
        mock_reporter.send_error_to_telegram.assert_not_called()

    def test_report_step_error_async_sends_telegram_when_enabled(self, monkeypatch):
        """ErrorReporter가 활성화 상태일 때 Telegram 알림 전송."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from app.tasks import kis as kis_tasks

        mock_reporter = MagicMock()
        mock_reporter._enabled = True
        mock_reporter.send_error_to_telegram = AsyncMock(return_value=True)

        monkeypatch.setattr(kis_tasks, "get_error_reporter", lambda: mock_reporter)

        # 함수 실행
        asyncio.run(
            kis_tasks._report_step_error_async(
                "kis.run_per_domestic_stock_automation",
                "삼성전자우",
                "005935",
                "매도",
                "unexpected keyword argument 'symbol'",
            )
        )

        # send_error_to_telegram이 호출되어야 함
        mock_reporter.send_error_to_telegram.assert_called_once()

        # 호출 인자 확인
        call_args = mock_reporter.send_error_to_telegram.call_args
        assert "unexpected keyword argument" in str(call_args.kwargs["error"])
        assert (
            call_args.kwargs["additional_context"]["task_name"]
            == "kis.run_per_domestic_stock_automation"
        )
        assert call_args.kwargs["additional_context"]["stock"] == "삼성전자우 (005935)"
        assert call_args.kwargs["additional_context"]["step"] == "매도"

    def test_automation_task_reports_error_on_exception(self, monkeypatch):
        """태스크에서 예외 발생 시 알림 함수가 호출되는지 확인."""
        from unittest.mock import AsyncMock, MagicMock, patch

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

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        error_reports = []

        async def fake_report_error(
            task_name, stock_name, stock_code, step_name, error_msg
        ):
            error_reports.append(
                {
                    "task_name": task_name,
                    "stock_name": stock_name,
                    "stock_code": stock_code,
                    "step_name": step_name,
                    "error_msg": error_msg,
                }
            )

        async def fake_buy(*_, **__):
            return {"success": True, "message": "매수 완료"}

        async def fake_sell(*_, **__):
            raise TypeError("unexpected keyword argument 'symbol'")

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks, "_report_step_error_async", fake_report_error
            )
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
        assert len(error_reports) == 1, (
            f"Expected 1 error report, got {len(error_reports)}"
        )
        assert error_reports[0]["task_name"] == "kis.run_per_domestic_stock_automation"
        assert error_reports[0]["stock_name"] == "삼성전자우"
        assert error_reports[0]["stock_code"] == "005935"
        assert error_reports[0]["step_name"] == "매도"
        assert "unexpected keyword argument" in error_reports[0]["error_msg"]

    def test_automation_task_reports_error_from_result(self, monkeypatch):
        """결과에 error 필드가 있을 때 알림이 전송되는지 확인."""
        from unittest.mock import AsyncMock, MagicMock, patch

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

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        error_reports = []

        async def fake_report_error(
            task_name, stock_name, stock_code, step_name, error_msg
        ):
            error_reports.append(
                {
                    "task_name": task_name,
                    "step_name": step_name,
                    "error_msg": error_msg,
                }
            )

        async def fake_buy(*_, **__):
            # 예외가 아닌 결과에 error 포함
            return {
                "success": False,
                "error": "DB connection failed",
                "orders_placed": 0,
            }

        async def fake_sell(*_, **__):
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks, "_report_step_error_async", fake_report_error
            )
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


class TestOverseasStockTelegramNotifications:
    """해외주식 태스크 텔레그램 알림 테스트."""

    def test_run_per_overseas_stock_automation_buy_failure_sends_telegram(
        self, monkeypatch
    ):
        """해외주식 매수 실패 시 텔레그램 알림이 전송되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "buy", "confidence": 80}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self, *args, **kwargs):
                return [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "애플",
                        "pchs_avg_pric": "170.00",
                        "now_pric2": "175.00",
                        "ovrs_cblc_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def cancel_overseas_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        telegram_notifications = []

        class MockNotifier:
            async def notify_trade_failure(
                self, symbol, korean_name, reason, market_type
            ):
                telegram_notifications.append(
                    {
                        "type": "failure",
                        "symbol": symbol,
                        "reason": reason,
                        "market_type": market_type,
                    }
                )
                return True

        async def fake_buy(*_, **__):
            raise RuntimeError("APBK0656 해당종목정보가 없습니다.")

        async def fake_sell(*_, **__):
            return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 매수 실패 시 텔레그램 알림이 전송되어야 함
        failure_notifications = [
            n for n in telegram_notifications if n["type"] == "failure"
        ]
        assert len(failure_notifications) >= 1, (
            f"Expected failure notification, got {telegram_notifications}"
        )
        assert failure_notifications[0]["symbol"] == "AAPL"
        assert "APBK0656" in failure_notifications[0]["reason"]
        assert failure_notifications[0]["market_type"] == "해외주식"

    def test_run_per_overseas_stock_automation_sell_failure_sends_telegram(
        self, monkeypatch
    ):
        """해외주식 매도 실패 시 텔레그램 알림이 전송되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "sell", "confidence": 85}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self, *args, **kwargs):
                return [
                    {
                        "ovrs_pdno": "VOO",
                        "ovrs_item_name": "VOO",
                        "pchs_avg_pric": "500.00",
                        "now_pric2": "520.00",
                        "ovrs_cblc_qty": "5",
                        "ovrs_excg_cd": "NYSE",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def cancel_overseas_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        telegram_notifications = []

        class MockNotifier:
            async def notify_trade_failure(
                self, symbol, korean_name, reason, market_type
            ):
                telegram_notifications.append(
                    {
                        "type": "failure",
                        "symbol": symbol,
                        "reason": reason,
                        "market_type": market_type,
                    }
                )
                return True

            async def notify_sell_order(self, **kwargs):
                telegram_notifications.append({"type": "sell_order", **kwargs})
                return True

        async def fake_buy(*_, **__):
            return {"success": False, "message": "매수 조건 미충족", "orders_placed": 0}

        async def fake_sell(*_, **__):
            raise RuntimeError("APBK0656 해당종목정보가 없습니다.")

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 매도 실패 시 텔레그램 알림이 전송되어야 함
        failure_notifications = [
            n for n in telegram_notifications if n["type"] == "failure"
        ]
        assert len(failure_notifications) >= 1, (
            f"Expected failure notification, got {telegram_notifications}"
        )

        sell_failure = next(
            (n for n in failure_notifications if "매도" in n["reason"]), None
        )
        assert sell_failure is not None, (
            f"Expected sell failure notification, got {failure_notifications}"
        )
        assert sell_failure["symbol"] == "VOO"
        assert sell_failure["market_type"] == "해외주식"

    def test_run_per_overseas_stock_automation_buy_success_sends_telegram(
        self, monkeypatch
    ):
        """해외주식 매수 성공 시 텔레그램 알림이 전송되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "buy", "confidence": 80}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self, *args, **kwargs):
                return [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "애플",
                        "pchs_avg_pric": "170.00",
                        "now_pric2": "175.00",
                        "ovrs_cblc_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def cancel_overseas_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        telegram_notifications = []

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                telegram_notifications.append({"type": "buy_order", **kwargs})
                return True

            async def notify_sell_order(self, **kwargs):
                telegram_notifications.append({"type": "sell_order", **kwargs})
                return True

            async def notify_trade_failure(self, **kwargs):
                telegram_notifications.append({"type": "failure", **kwargs})
                return True

        async def fake_buy(*_, **__):
            return {
                "success": True,
                "message": "매수 완료",
                "orders_placed": 2,
                "total_amount": 350.0,
                "prices": [172.0, 170.0],
                "quantities": [1, 1],
            }

        async def fake_sell(*_, **__):
            return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 매수 성공 시 텔레그램 알림이 전송되어야 함
        buy_notifications = [
            n for n in telegram_notifications if n["type"] == "buy_order"
        ]
        assert len(buy_notifications) >= 1, (
            f"Expected buy notification, got {telegram_notifications}"
        )
        assert buy_notifications[0]["symbol"] == "AAPL"
        assert buy_notifications[0]["order_count"] == 2
        assert buy_notifications[0]["market_type"] == "해외주식"

    def test_run_per_overseas_stock_automation_sell_success_sends_telegram(
        self, monkeypatch
    ):
        """해외주식 매도 성공 시 텔레그램 알림이 전송되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "sell", "confidence": 85}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self, *args, **kwargs):
                return [
                    {
                        "ovrs_pdno": "TSLA",
                        "ovrs_item_name": "테슬라",
                        "pchs_avg_pric": "200.00",
                        "now_pric2": "250.00",
                        "ovrs_cblc_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def cancel_overseas_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        telegram_notifications = []

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                telegram_notifications.append({"type": "buy_order", **kwargs})
                return True

            async def notify_sell_order(self, **kwargs):
                telegram_notifications.append({"type": "sell_order", **kwargs})
                return True

            async def notify_trade_failure(self, **kwargs):
                telegram_notifications.append({"type": "failure", **kwargs})
                return True

        async def fake_buy(*_, **__):
            return {"success": False, "message": "매수 조건 미충족", "orders_placed": 0}

        async def fake_sell(*_, **__):
            return {
                "success": True,
                "message": "매도 완료",
                "orders_placed": 2,
                "total_volume": 5,
                "prices": [255.0, 260.0],
                "quantities": [2, 3],
                "expected_amount": 1290.0,
            }

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 매도 성공 시 텔레그램 알림이 전송되어야 함
        sell_notifications = [
            n for n in telegram_notifications if n["type"] == "sell_order"
        ]
        assert len(sell_notifications) >= 1, (
            f"Expected sell notification, got {telegram_notifications}"
        )
        assert sell_notifications[0]["symbol"] == "TSLA"
        assert sell_notifications[0]["order_count"] == 2
        assert sell_notifications[0]["market_type"] == "해외주식"

    def test_run_per_overseas_stock_automation_cancels_pending_orders(
        self, monkeypatch
    ):
        """해외주식 자동화 시 미체결 주문이 취소되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "buy", "confidence": 80}, "gemini-2.5-pro"

            async def close(self):
                return None

        cancelled_orders = []

        class DummyKIS:
            async def fetch_my_overseas_stocks(self, *args, **kwargs):
                return [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "애플",
                        "pchs_avg_pric": "170.00",
                        "now_pric2": "175.00",
                        "ovrs_cblc_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                # 기존 미체결 매수/매도 주문 시뮬레이션
                return [
                    {
                        "pdno": "AAPL",
                        "odno": "ORDER001",
                        "sll_buy_dvsn_cd": "02",  # 매수
                        "ft_ord_qty": "5",
                    },
                    {
                        "pdno": "AAPL",
                        "odno": "ORDER002",
                        "sll_buy_dvsn_cd": "01",  # 매도
                        "ft_ord_qty": "3",
                    },
                ]

            async def cancel_overseas_order(
                self, order_number, symbol, exchange_code, quantity, is_mock
            ):
                cancelled_orders.append(
                    {
                        "order_number": order_number,
                        "symbol": symbol,
                        "quantity": quantity,
                    }
                )
                return {"odno": order_number}

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                return True

            async def notify_sell_order(self, **kwargs):
                return True

            async def notify_trade_failure(self, **kwargs):
                return True

        async def fake_buy(*_, **__):
            return {
                "success": True,
                "orders_placed": 1,
                "total_amount": 175.0,
                "prices": [175.0],
                "quantities": [1],
            }

        async def fake_sell(*_, **__):
            return {
                "success": True,
                "orders_placed": 1,
                "total_volume": 1,
                "prices": [180.0],
                "quantities": [1],
                "expected_amount": 180.0,
            }

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 미체결 주문 취소 확인 (매수 1개, 매도 1개)
        assert len(cancelled_orders) == 2, (
            f"Expected 2 cancelled orders, got {cancelled_orders}"
        )
        buy_cancels = [o for o in cancelled_orders if o["order_number"] == "ORDER001"]
        sell_cancels = [o for o in cancelled_orders if o["order_number"] == "ORDER002"]
        assert len(buy_cancels) == 1, "Buy order should be cancelled"
        assert len(sell_cancels) == 1, "Sell order should be cancelled"


class TestDomesticStockPendingOrderCancel:
    """국내주식 미체결 주문 취소 테스트."""

    def test_run_per_domestic_stock_automation_cancels_pending_orders(
        self, monkeypatch
    ):
        """국내주식 자동화 시 미체결 주문이 취소되어야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "buy", "confidence": 80}, "gemini-2.5-pro"

            async def close(self):
                return None

        cancelled_orders = []

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

            async def inquire_korea_orders(self, *args, **kwargs):
                # 기존 미체결 매수/매도 주문 시뮬레이션
                return [
                    {
                        "pdno": "005930",
                        "ord_no": "ORDER001",
                        "sll_buy_dvsn_cd": "02",  # 매수
                        "ord_qty": "5",
                        "ord_unpr": "49000",
                    },
                    {
                        "pdno": "005930",
                        "ord_no": "ORDER002",
                        "sll_buy_dvsn_cd": "01",  # 매도
                        "ord_qty": "3",
                        "ord_unpr": "55000",
                    },
                ]

            async def cancel_korea_order(
                self, order_number, stock_code, quantity, price, order_type, is_mock
            ):
                cancelled_orders.append(
                    {
                        "order_number": order_number,
                        "stock_code": stock_code,
                        "quantity": quantity,
                        "order_type": order_type,
                    }
                )
                return {"odno": order_number}

        # Mock ManualHoldingsService to return empty list
        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        async def fake_buy(*_, **__):
            return {
                "success": True,
                "orders_placed": 1,
                "total_amount": 50000.0,
                "prices": [50000],
                "quantities": [1],
            }

        async def fake_sell(*_, **__):
            return {
                "success": True,
                "orders_placed": 1,
                "total_volume": 1,
                "prices": [55000],
                "quantities": [1],
                "expected_amount": 55000.0,
            }

        # Mock DB session
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        assert result["status"] == "completed"

        # 미체결 주문 취소 확인 (매수 1개, 매도 1개)
        assert len(cancelled_orders) == 2, (
            f"Expected 2 cancelled orders, got {cancelled_orders}"
        )
        buy_cancels = [o for o in cancelled_orders if o["order_number"] == "ORDER001"]
        sell_cancels = [o for o in cancelled_orders if o["order_number"] == "ORDER002"]
        assert len(buy_cancels) == 1, "Buy order should be cancelled"
        assert buy_cancels[0]["order_type"] == "buy"
        assert len(sell_cancels) == 1, "Sell order should be cancelled"
        assert sell_cancels[0]["order_type"] == "sell"


class TestOrderableQuantityUsage:
    """주문 가능 수량(ord_psbl_qty) 사용 테스트.

    실제 버그 시나리오: 보유 8주, 미체결 매도 3주 → 실제 주문 가능 5주
    hldg_qty(8) 대신 ord_psbl_qty(5)를 사용해야 주문 수량 초과 에러를 방지할 수 있음.
    """

    def test_domestic_automation_uses_orderable_qty_for_sell(self, monkeypatch):
        """매도 시 hldg_qty가 아닌 ord_psbl_qty를 사용해야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        sell_qty_received = []

        class DummyKIS:
            def __init__(self):
                self.fetch_count = 0

            async def fetch_my_stocks(self):
                self.fetch_count += 1
                # 첫 번째 호출: 보유 8주, 주문 가능 5주 (미체결 3주)
                # 두 번째 호출 (매수 후 리프레시): 동일
                return [
                    {
                        "pdno": "005935",
                        "prdt_name": "삼성전자우",
                        "pchs_avg_pric": "76300",
                        "prpr": "77500",
                        "hldg_qty": "8",  # 총 보유 수량
                        "ord_psbl_qty": "5",  # 실제 주문 가능 수량
                    }
                ]

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        async def fake_buy(*_, **__):
            return {
                "success": False,
                "message": "1% 매수 조건 미충족",
                "orders_placed": 0,
            }

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_qty_received.append(qty)
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        assert result["status"] == "completed"
        assert len(sell_qty_received) == 1

        # 핵심 검증: 매도 함수에 전달된 수량이 ord_psbl_qty(5)여야 함
        # hldg_qty(8)가 전달되면 버그가 있는 것임
        assert sell_qty_received[0] == 5, (
            f"매도 시 ord_psbl_qty(5)를 사용해야 하는데 {sell_qty_received[0]}가 전달됨"
        )

    def test_domestic_automation_refresh_uses_orderable_qty(self, monkeypatch):
        """매수 후 잔고 재조회 시에도 ord_psbl_qty를 사용해야 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "buy", "confidence": 80}, "gemini-2.5-pro"

            async def close(self):
                return None

        sell_qty_received = []

        class DummyKIS:
            def __init__(self):
                self.fetch_count = 0

            async def fetch_my_stocks(self):
                self.fetch_count += 1
                if self.fetch_count == 1:
                    # 최초 조회
                    return [
                        {
                            "pdno": "005930",
                            "prdt_name": "삼성전자",
                            "pchs_avg_pric": "50000",
                            "prpr": "49000",  # 현재가 < 평단가*0.99 → 매수 조건 충족
                            "hldg_qty": "10",
                            "ord_psbl_qty": "7",  # 미체결 3주
                        }
                    ]
                else:
                    # 매수 후 재조회 - 보유 증가, 주문 가능도 증가
                    return [
                        {
                            "pdno": "005930",
                            "prdt_name": "삼성전자",
                            "pchs_avg_pric": "49500",
                            "prpr": "49500",
                            "hldg_qty": "12",  # 2주 추가 매수
                            "ord_psbl_qty": "9",  # 9주 주문 가능
                        }
                    ]

            async def inquire_korea_orders(self, *args, **kwargs):
                return []

            async def cancel_korea_order(self, *args, **kwargs):
                return {"odno": "0000001"}

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        async def fake_buy(*_, **__):
            return {"success": True, "message": "매수 완료", "orders_placed": 2}

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_qty_received.append(qty)
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        assert result["status"] == "completed"
        assert len(sell_qty_received) == 1

        # 재조회 후 ord_psbl_qty(9)가 전달되어야 함
        assert sell_qty_received[0] == 9, (
            f"재조회 후 ord_psbl_qty(9)를 사용해야 하는데 {sell_qty_received[0]}가 전달됨"
        )

    def test_domestic_automation_refreshes_qty_after_sell_order_cancel(
        self, monkeypatch
    ):
        """미체결 매도 주문 취소 후 잔고를 재조회하여 ord_psbl_qty를 갱신해야 함.

        실제 버그 시나리오 (APBK0986 에러):
        - 보유 8주, 미체결 매도 3주 → ord_psbl_qty=5
        - 매수 후 잔고 재조회 → ord_psbl_qty=5 (미체결 매도 3주 여전히 존재)
        - 미체결 매도 3주 취소 → 실제로는 ord_psbl_qty=8이 되어야 함
        - 기존 코드: 취소 후 재조회 없이 5주로 매도 시도
        - 수정 후: 취소 후 재조회하여 8주로 매도 시도
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.tasks import kis as kis_tasks

        class DummyAnalyzer:
            async def analyze_stock_json(self, name):
                return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

            async def close(self):
                return None

        sell_qty_received = []

        class DummyKIS:
            def __init__(self):
                self.fetch_count = 0
                self.cancel_sell_called = False

            async def fetch_my_stocks(self):
                self.fetch_count += 1
                if self.fetch_count <= 2:
                    # 첫 번째/두 번째 호출: 미체결 매도 3주가 있어서 ord_psbl_qty=5
                    return [
                        {
                            "pdno": "005935",
                            "prdt_name": "삼성전자우",
                            "pchs_avg_pric": "76300",
                            "prpr": "77500",
                            "hldg_qty": "8",
                            "ord_psbl_qty": "5",  # 미체결 매도 3주가 있는 상태
                        }
                    ]
                else:
                    # 세 번째 호출 (미체결 매도 취소 후): ord_psbl_qty=8
                    return [
                        {
                            "pdno": "005935",
                            "prdt_name": "삼성전자우",
                            "pchs_avg_pric": "76300",
                            "prpr": "77500",
                            "hldg_qty": "8",
                            "ord_psbl_qty": "8",  # 미체결 매도 취소 후
                        }
                    ]

            async def inquire_korea_orders(self, *args, **kwargs):
                # 미체결 매도 주문 3주가 있음
                return [
                    {
                        "pdno": "005935",
                        "sll_buy_dvsn_cd": "01",  # 매도
                        "odno": "0000001",
                        "ord_qty": "3",
                        "ord_unpr": "78000",
                    }
                ]

            async def cancel_korea_order(self, *args, **kwargs):
                self.cancel_sell_called = True
                return {"odno": "0000001"}

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return []

        async def fake_buy(*_, **__):
            return {
                "success": False,
                "message": "1% 매수 조건 미충족",
                "orders_placed": 0,
            }

        async def fake_sell(kis, symbol, current_price, avg_price, qty):
            sell_qty_received.append(qty)
            return {"success": True, "message": "매도 완료", "orders_placed": 1}

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(kis_tasks, "KISAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_domestic_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(
                kis_tasks.run_per_domestic_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_domestic_stock_automation.apply().result

        assert result["status"] == "completed"
        assert len(sell_qty_received) == 1

        # 핵심 검증: 미체결 매도 취소 후 잔고를 재조회하여 ord_psbl_qty(8)가 전달되어야 함
        # 재조회 없이 기존 ord_psbl_qty(5)가 전달되면 버그가 있는 것임
        assert sell_qty_received[0] == 8, (
            f"미체결 매도 취소 후 재조회하여 ord_psbl_qty(8)를 사용해야 하는데 {sell_qty_received[0]}가 전달됨"
        )


class TestOverseasManualHoldings:
    """해외주식 토스 전용 종목(수동 잔고) 테스트."""

    def test_overseas_automation_includes_manual_holdings(self, monkeypatch):
        """해외주식 자동화 시 토스 전용 종목도 포함되어 분석/매수가 실행되어야 함."""
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock, patch

        import pandas as pd

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        analyzed_symbols = []
        buy_calls = []

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                analyzed_symbols.append(symbol)
                return {"decision": "buy", "confidence": 75}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self):
                # KIS 계좌에는 AAPL만 있음
                return [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "애플",
                        "pchs_avg_pric": "170.00",
                        "now_pric2": "175.00",
                        "ovrs_cblc_qty": "10",
                        "ord_psbl_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def inquire_overseas_price(self, symbol):
                # CONY 현재가 조회
                return pd.DataFrame([{"close": 18.50}])

        # 토스에 CONY 보유 (KIS에 없음)
        class MockManualHolding:
            ticker = "CONY"
            display_name = "CONY"
            quantity = Decimal("20")
            avg_price = Decimal("17.18")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [MockManualHolding()]

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                return True

            async def notify_sell_order(self, **kwargs):
                return True

            async def notify_trade_failure(self, **kwargs):
                return True

        async def fake_buy(kis, symbol, current_price, avg_price):
            buy_calls.append({"symbol": symbol, "current_price": current_price})
            return {
                "success": True,
                "orders_placed": 1,
                "total_amount": 100.0,
                "prices": [100.0],
                "quantities": [1],
            }

        async def fake_sell(*_, **__):
            return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

        # Mock _send_toss_recommendation_async
        toss_recommendations = []

        async def mock_toss_recommendation(**kwargs):
            toss_recommendations.append(kwargs)

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks, "_send_toss_recommendation_async", mock_toss_recommendation
            )
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # AAPL(KIS)과 CONY(토스) 모두 분석되어야 함
        assert "AAPL" in analyzed_symbols, "KIS 종목 AAPL이 분석되어야 함"
        assert "CONY" in analyzed_symbols, "토스 종목 CONY가 분석되어야 함"

        # 매수는 두 종목 모두 시도
        assert len(buy_calls) == 2, (
            f"KIS와 토스 종목 모두 매수 시도해야 함, 실제: {buy_calls}"
        )

        # CONY의 현재가가 API로 조회되어 설정되었는지 확인
        cony_buy = next((b for b in buy_calls if b["symbol"] == "CONY"), None)
        assert cony_buy is not None
        assert cony_buy["current_price"] == 18.50, (
            "토스 종목 현재가가 API로 조회되어야 함"
        )

    def test_overseas_automation_skips_sell_for_manual_holdings(self, monkeypatch):
        """토스 전용 종목은 매도를 스킵하고 추천 알림만 발송해야 함."""
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock, patch

        import pandas as pd

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        sell_calls = []
        toss_recommendations = []

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                return {"decision": "sell", "confidence": 85}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self):
                # KIS 계좌에는 아무것도 없음
                return []

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

            async def inquire_overseas_price(self, symbol):
                return pd.DataFrame([{"close": 18.50}])

        # 토스에만 CONY 보유
        class MockManualHolding:
            ticker = "CONY"
            display_name = "CONY"
            quantity = Decimal("20")
            avg_price = Decimal("17.18")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [MockManualHolding()]

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                return True

            async def notify_sell_order(self, **kwargs):
                return True

            async def notify_trade_failure(self, **kwargs):
                return True

        async def fake_buy(*_, **__):
            return {"success": False, "message": "매수 조건 미충족", "orders_placed": 0}

        async def fake_sell(kis, symbol, current_price, avg_price, qty, exchange_code):
            sell_calls.append({"symbol": symbol})
            return {"success": True, "orders_placed": 1}

        async def mock_toss_recommendation(**kwargs):
            toss_recommendations.append(kwargs)

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks, "_send_toss_recommendation_async", mock_toss_recommendation
            )
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # 토스 종목은 KIS에서 매도할 수 없으므로 매도 함수가 호출되면 안됨
        assert len(sell_calls) == 0, (
            f"토스 종목은 매도 스킵해야 함, 실제 매도 호출: {sell_calls}"
        )

        # 대신 토스 추천 알림이 발송되어야 함
        assert len(toss_recommendations) == 1, (
            f"토스 추천 알림이 발송되어야 함, 실제: {toss_recommendations}"
        )
        assert toss_recommendations[0]["code"] == "CONY"

        # 결과에 '수동잔고' 매도 스킵 정보가 포함되어야 함
        cony_result = next(
            (r for r in result["results"] if r["symbol"] == "CONY"), None
        )
        assert cony_result is not None
        sell_step = next((s for s in cony_result["steps"] if s["step"] == "매도"), None)
        assert sell_step is not None
        assert "수동잔고" in sell_step["result"]["message"]

    def test_overseas_automation_does_not_duplicate_kis_stocks(self, monkeypatch):
        """KIS에도 있고 토스에도 있는 종목은 중복 추가되지 않아야 함."""
        from decimal import Decimal
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.analysis import service_analyzers
        from app.tasks import kis as kis_tasks

        analyzed_symbols = []

        class DummyAnalyzer:
            async def analyze_stock_json(self, symbol):
                analyzed_symbols.append(symbol)
                return {"decision": "hold", "confidence": 70}, "gemini-2.5-pro"

            async def close(self):
                return None

        class DummyKIS:
            async def fetch_my_overseas_stocks(self):
                # KIS에 AAPL 10주
                return [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "애플",
                        "pchs_avg_pric": "170.00",
                        "now_pric2": "175.00",
                        "ovrs_cblc_qty": "10",
                        "ord_psbl_qty": "10",
                        "ovrs_excg_cd": "NASD",
                    }
                ]

            async def inquire_overseas_orders(self, *args, **kwargs):
                return []

        # 토스에도 AAPL 5주 (중복)
        class MockManualHolding:
            ticker = "AAPL"  # KIS에도 있는 종목
            display_name = "애플"
            quantity = Decimal("5")
            avg_price = Decimal("165.00")

        class MockManualService:
            def __init__(self, db):
                pass

            async def get_holdings_by_user(self, user_id, market_type):
                return [MockManualHolding()]

        class MockNotifier:
            async def notify_buy_order(self, **kwargs):
                return True

            async def notify_sell_order(self, **kwargs):
                return True

            async def notify_trade_failure(self, **kwargs):
                return True

        async def fake_buy(*_, **__):
            return {"success": False, "message": "매수 조건 미충족", "orders_placed": 0}

        async def fake_sell(*_, **__):
            return {"success": False, "message": "매도 조건 미충족", "orders_placed": 0}

        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session),
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                MockManualService,
            ),
        ):
            monkeypatch.setattr(kis_tasks, "KISClient", DummyKIS)
            monkeypatch.setattr(service_analyzers, "YahooAnalyzer", DummyAnalyzer)
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_buy_orders_with_analysis", fake_buy
            )
            monkeypatch.setattr(
                kis_tasks, "process_kis_overseas_sell_orders_with_analysis", fake_sell
            )
            monkeypatch.setattr(kis_tasks, "get_trade_notifier", lambda: MockNotifier())
            monkeypatch.setattr(
                kis_tasks.run_per_overseas_stock_automation,
                "update_state",
                lambda *_, **__: None,
                raising=False,
            )

            result = kis_tasks.run_per_overseas_stock_automation.apply().result

        assert result["status"] == "completed"

        # AAPL은 한 번만 분석되어야 함 (중복 추가 안됨)
        aapl_count = analyzed_symbols.count("AAPL")
        assert aapl_count == 1, (
            f"KIS에 있는 AAPL은 한 번만 분석되어야 함, 실제: {aapl_count}번"
        )

        # 결과에도 AAPL은 한 번만 포함
        assert len(result["results"]) == 1
