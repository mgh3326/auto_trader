"""
Tests for Celery tasks defined in app.tasks.analyze.
"""
import pytest


class DummyTask:
    """Lightweight stand-in for a Celery task instance."""

    def __init__(self):
        self.states = []

    def update_state(self, **kwargs):
        self.states.append(kwargs)


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
        analyze.upbit_pairs,
        "KRW_TRADABLE_COINS",
        {"BTC"},
    )
    monkeypatch.setattr(
        analyze.upbit_pairs,
        "COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
    )
    analyzers = _patch_upbit_analyzer(monkeypatch, tradable=False)

    task = DummyTask()
    raw = analyze.run_analysis_for_my_coins.__wrapped__.__func__
    result = raw(task)

    assert result["status"] == "completed"
    assert result["analyzed_count"] == 0
    assert result["total_count"] == 0
    assert result["results"] == []
    assert analyzers and analyzers[0].closed is True
    assert any(state["state"] == "PROGRESS" for state in task.states)


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
        analyze.upbit_pairs,
        "KRW_TRADABLE_COINS",
        {"BTC"},
    )
    monkeypatch.setattr(
        analyze.upbit_pairs,
        "COIN_TO_NAME_KR",
        {"BTC": "비트코인"},
    )
    analyzers = _patch_upbit_analyzer(monkeypatch, tradable=False)

    task = DummyTask()
    raw = analyze.execute_buy_orders_task.__wrapped__.__func__
    result = raw(task)

    assert result["status"] == "completed"
    assert result["success_count"] == 0
    assert result["total_count"] == 0
    assert result["results"] == []
    assert analyzers and analyzers[0].closed is True
    assert any(state["state"] == "PROGRESS" for state in task.states)
