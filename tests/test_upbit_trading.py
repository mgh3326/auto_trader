"""
Tests covering the Upbit trading router endpoints.
"""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_get_my_coins_success(monkeypatch):
    """보유 코인 조회가 정상 구조로 응답하는지 확인."""
    from app.routers import upbit_trading

    async def fake_prime():
        return None

    sample_coins = [
        {
            "currency": "KRW",
            "balance": "100000",
            "locked": "0",
        },
        {
            "currency": "BTC",
            "balance": "0.01",
            "locked": "0.0",
            "avg_buy_price": "50000000",
        },
    ]

    async def fake_fetch_my_coins():
        return sample_coins

    class DummyAnalyzer:
        def __init__(self):
            self.closed = False

        def _is_tradable(self, coin):
            return coin.get("currency") != "KRW"

        async def close(self):
            self.closed = True

    async def fake_fetch_prices(markets):
        return {market: 60000000 for market in markets}

    class DummyAnalysisService:
        def __init__(self, db):
            self.db = db

        async def get_latest_analysis_results_for_coins(self, markets):
            return {market: None for market in markets}

    monkeypatch.setattr(
        "data.coins_info.upbit_pairs.prime_upbit_constants",
        fake_prime,
    )
    monkeypatch.setattr(
        "app.services.upbit.fetch_my_coins",
        fake_fetch_my_coins,
    )
    monkeypatch.setattr(
        "app.services.upbit.fetch_multiple_current_prices",
        fake_fetch_prices,
    )
    monkeypatch.setattr(
        upbit_trading.upbit_pairs,
        "KRW_TRADABLE_COINS",
        {"BTC"},
    )
    monkeypatch.setattr(
        upbit_trading.upbit_pairs,
        "COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
    )
    monkeypatch.setattr(
        upbit_trading,
        "UpbitAnalyzer",
        DummyAnalyzer,
    )
    monkeypatch.setattr(
        upbit_trading,
        "StockAnalysisService",
        DummyAnalysisService,
    )

    response = await upbit_trading.get_my_coins(db=object())

    assert response["success"] is True
    assert response["tradable_coins_count"] == 1
    assert isinstance(response["coins"], list)
    assert response["coins"][0]["currency"] == "BTC"
    assert response["coins"][0]["current_price"] == 60000000


@pytest.mark.asyncio
async def test_execute_buy_orders_triggers_celery(monkeypatch):
    """매수 작업이 Celery 태스크를 enqueue 하는지 확인."""
    from app.routers import upbit_trading

    class DummyResult:
        def __init__(self):
            self.id = "task-123"

    class DummyCelery:
        def __init__(self):
            self.called_with = None

        def send_task(self, name, args=None):
            self.called_with = (name, args)
            return DummyResult()

    dummy_celery = DummyCelery()

    monkeypatch.setattr(
        "app.core.celery_app.celery_app",
        dummy_celery,
    )
    monkeypatch.setattr(
        upbit_trading,
        "settings",
        SimpleNamespace(upbit_access_key="key", upbit_secret_key="secret"),
    )

    response = await upbit_trading.execute_buy_orders()

    assert response["success"] is True
    assert response["task_id"] == "task-123"
    assert dummy_celery.called_with[0] == "upbit.execute_buy_orders"
