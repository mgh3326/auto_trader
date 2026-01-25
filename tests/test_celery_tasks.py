"""Tests for Celery tasks defined in app.tasks.analyze."""

import pytest


def _patch_upbit_analyzer(monkeypatch, *, tradable: bool):
    """Patch UpbitAnalyzer to simplify task execution."""
    from app.tasks import analyze

    created = []

    class DummyAnalyzer:
        def __init__(self):
            self.closed = False
            created.append(self)

        def _is_tradable(self, coin):
            return tradable

        def is_tradable(self, coin):
            return self._is_tradable(coin)

        async def analyze_coins_json(self, names):
            return {"status": "ok"}, "model"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(analyze, "UpbitAnalyzer", DummyAnalyzer)
    return created


def test_run_analysis_for_my_coins_no_tradable(monkeypatch):
    """거래 가능한 코인이 없을 때 완료 상태로 즉시 반환하는지 확인."""
    from app.tasks import analyze

    async def fake_prime():
        return None

    async def fake_fetch_my_coins():
        return [{"currency": "KRW", "balance": "100000"}]

    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.prime_upbit_constants",
        fake_prime,
    )
    monkeypatch.setattr(
        "app.services.upbit.fetch_my_coins",
        fake_fetch_my_coins,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.KRW_TRADABLE_COINS",
        {"BTC"},
        raising=False,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
        raising=False,
    )
    analyzers = _patch_upbit_analyzer(monkeypatch, tradable=False)

    progress_updates = []

    def record_update(*_, **kwargs):
        progress_updates.append(
            {
                "state": kwargs.get("state"),
                "meta": kwargs.get("meta"),
            }
        )

    monkeypatch.setattr(
        analyze.run_analysis_for_my_coins,
        "update_state",
        record_update,
        raising=False,
    )

    result = analyze.run_analysis_for_my_coins.apply().result

    assert result["status"] == "completed"
    assert result["analyzed_count"] == 0
    assert result["total_count"] == 0
    assert result["results"] == []
    assert analyzers and analyzers[0].closed is True
    assert any(update["state"] == "PROGRESS" for update in progress_updates)


def test_execute_buy_orders_task_no_tradable(monkeypatch):
    """매수 태스크가 거래 가능한 코인이 없으면 즉시 종료하는지 확인."""
    from app.tasks import analyze

    async def fake_prime():
        return None

    async def fake_fetch_my_coins():
        return [{"currency": "KRW", "balance": "100000"}]

    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.prime_upbit_constants",
        fake_prime,
    )
    monkeypatch.setattr(
        "app.services.upbit.fetch_my_coins",
        fake_fetch_my_coins,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.KRW_TRADABLE_COINS",
        {"BTC"},
        raising=False,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
        raising=False,
    )
    analyzers = _patch_upbit_analyzer(monkeypatch, tradable=False)

    progress_updates = []

    def record_update(*_, **kwargs):
        progress_updates.append(
            {
                "state": kwargs.get("state"),
                "meta": kwargs.get("meta"),
            }
        )

    monkeypatch.setattr(
        analyze.execute_buy_orders_task,
        "update_state",
        record_update,
        raising=False,
    )

    result = analyze.execute_buy_orders_task.apply().result

    assert result["status"] == "completed"
    assert result["success_count"] == 0
    assert result["total_count"] == 0
    assert result["results"] == []
    assert analyzers and analyzers[0].closed is True
    assert any(update["state"] == "PROGRESS" for update in progress_updates)


@pytest.mark.asyncio
async def test_execute_buy_order_notifies_on_insufficient_balance(monkeypatch):
    """잔고 부족으로 매수 실패 시 텔레그램 알림을 보내는지 확인."""
    from app.tasks import analyze

    async def fake_prime():
        return None

    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.prime_upbit_constants",
        fake_prime,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.KRW_TRADABLE_COINS",
        {"BTC"},
        raising=False,
    )
    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
        raising=False,
    )

    async def fake_fetch_my_coins():
        return [{"currency": "BTC", "avg_buy_price": "1000000", "balance": "0.1"}]

    monkeypatch.setattr("app.services.upbit.fetch_my_coins", fake_fetch_my_coins)

    class DummyPriceFrame:
        def __init__(self, close):
            self._row = {"close": close}

        @property
        def iloc(self):
            return self

        def __getitem__(self, index):
            if index == 0:
                return self._row
            raise IndexError

    async def fake_fetch_price(_):
        return DummyPriceFrame(close=900000)

    monkeypatch.setattr("app.services.upbit.fetch_price", fake_fetch_price)

    async def fake_cancel(_):
        return None

    monkeypatch.setattr(analyze, "cancel_existing_buy_orders", fake_cancel)
    monkeypatch.setattr(analyze.asyncio, "sleep", fake_cancel)

    failure_message = "모든 매수 주문 실패: 주문 가능 금액 부족"

    async def fake_process_buy_orders_with_analysis(*_, **__):
        return {
            "success": False,
            "message": failure_message,
            "orders_placed": 0,
            "total_amount": 0.0,
        }

    monkeypatch.setattr(
        "app.services.stock_info_service.process_buy_orders_with_analysis",
        fake_process_buy_orders_with_analysis,
    )

    class DummyNotifier:
        def __init__(self):
            self.failure_calls = []

        async def notify_trade_failure(
            self, symbol, korean_name, reason, market_type="암호화폐"
        ):
            self.failure_calls.append(
                {
                    "symbol": symbol,
                    "korean_name": korean_name,
                    "reason": reason,
                    "market_type": market_type,
                }
            )
            return True

    dummy_notifier = DummyNotifier()
    monkeypatch.setattr(analyze, "get_trade_notifier", lambda: dummy_notifier)

    result = await analyze._execute_buy_order_for_coin_async("btc")

    assert result["status"] == "failed"
    assert dummy_notifier.failure_calls == [
        {
            "symbol": "BTC",
            "korean_name": "비트코인",
            "reason": failure_message,
            "market_type": "암호화폐",
        }
    ]


def test_run_per_coin_automation_no_tradable(monkeypatch):
    """코인 자동 실행 태스크가 거래 가능한 코인이 없을 때 바로 완료된다."""
    from app.tasks import analyze

    async def fake_fetch():
        return ([], [])

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(analyze, "_fetch_tradable_coins", fake_fetch)
    monkeypatch.setattr(analyze.asyncio, "sleep", fake_sleep)

    progress_updates = []

    def record_update(*_, **kwargs):
        progress_updates.append(
            {
                "state": kwargs.get("state"),
                "meta": kwargs.get("meta"),
            }
        )

    monkeypatch.setattr(
        analyze.run_per_coin_automation_task,
        "update_state",
        record_update,
        raising=False,
    )

    result = analyze.run_per_coin_automation_task.apply().result

    assert result["status"] == "completed"
    assert result["total_coins"] == 0
    assert result["success_coins"] == 0
    assert result["results"] == []
    assert progress_updates == []


def test_run_per_coin_automation_success(monkeypatch):
    """코인 자동 실행 태스크가 각 단계를 순서대로 호출하는지 확인."""
    from app.tasks import analyze

    async def fake_fetch():
        return (
            [{"currency": "BTC", "korean_name": "비트코인"}],
            [{"currency": "BTC", "korean_name": "비트코인"}],
        )

    async def fake_analyze(currency, progress_cb=None):
        if progress_cb:
            progress_cb({"status": f"{currency} 분석 중", "currency": currency})
        return {"status": "completed", "message": "분석 완료"}

    async def fake_buy(currency):
        return {"status": "completed", "message": "매수 완료"}

    async def fake_sell(currency):
        return {"status": "completed", "message": "매도 완료"}

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(analyze, "_fetch_tradable_coins", fake_fetch)
    monkeypatch.setattr(analyze, "_analyze_coin_async", fake_analyze)
    monkeypatch.setattr(analyze, "_execute_buy_order_for_coin_async", fake_buy)
    monkeypatch.setattr(analyze, "_execute_sell_order_for_coin_async", fake_sell)
    monkeypatch.setattr(analyze.asyncio, "sleep", fake_sleep)

    progress_updates = []

    def record_update(*_, **kwargs):
        progress_updates.append(
            {
                "state": kwargs.get("state"),
                "meta": kwargs.get("meta"),
            }
        )

    monkeypatch.setattr(
        analyze.run_per_coin_automation_task,
        "update_state",
        record_update,
        raising=False,
    )

    result = analyze.run_per_coin_automation_task.apply().result

    assert result["status"] == "completed"
    assert result["total_coins"] == 1
    assert result["success_coins"] == 1
    assert len(result["results"]) == 1
    coin_steps = result["results"][0]["steps"]
    assert [step["step"] for step in coin_steps] == ["analysis", "buy", "sell"]
    assert all(step["result"]["status"] == "completed" for step in coin_steps)
    assert any(
        update["meta"]["current_step"] == "analysis" for update in progress_updates
    )
