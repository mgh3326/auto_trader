"""Tests for KIS-related Celery tasks."""
from typing import Any, Dict, List


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
