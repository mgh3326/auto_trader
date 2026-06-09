"""ROB-404 — kis_mock execution-event consumer message handling."""

from __future__ import annotations

import json

import pytest

from app.services.kis_mock_execution_consumer import KISMockExecutionConsumer


class _FakeRedis:
    """Minimal SETNX-style fake: set(nx=True) returns True once per key."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _session_factory():
    return _FakeSession()


def _make_consumer(force_dry_run=False):
    calls: list[dict] = []

    async def _fake_reconcile(db, *, symbol=None, dry_run=True, **kw):
        calls.append({"symbol": symbol, "dry_run": dry_run})
        return {"success": True, "orders_processed": 0}

    consumer = KISMockExecutionConsumer(
        redis_client=_FakeRedis(),
        reconcile_fn=_fake_reconcile,
        session_factory=_session_factory,
        force_dry_run=force_dry_run,
    )
    return consumer, calls


def _fill_event(**over):
    event = {
        "account_mode": "kis_mock",
        "broker": "kis",
        "fill_yn": "Y",
        "execution_type": "1",
        "symbol": "005930",
        "correlation_id": "corr-1",
    }
    event.update(over)
    return json.dumps(event)


@pytest.mark.asyncio
async def test_kis_mock_fill_triggers_reconcile_for_symbol(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled"
    assert calls == [{"symbol": "005930", "dry_run": False}]


@pytest.mark.asyncio
async def test_gate_off_runs_dry_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        False,
        raising=False,
    )
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled_dry_run"
    assert calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_live_event_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(_fill_event(account_mode="kis_live"))
    assert outcome == "ignored_non_mock_fill"
    assert calls == []


@pytest.mark.asyncio
async def test_non_fill_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    outcome = await consumer.handle_message(
        _fill_event(fill_yn="N", execution_type="0")
    )
    assert outcome == "ignored_non_mock_fill"
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_correlation_id_skipped(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer()
    first = await consumer.handle_message(_fill_event())
    second = await consumer.handle_message(_fill_event())
    assert first == "reconciled"
    assert second == "skipped_dedup"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_correlation_id_skipped(monkeypatch):
    consumer, calls = _make_consumer()
    event = json.loads(_fill_event())
    event.pop("correlation_id")
    outcome = await consumer.handle_message(json.dumps(event))
    assert outcome == "ignored_no_correlation_id"
    assert calls == []


@pytest.mark.asyncio
async def test_unparseable_ignored(monkeypatch):
    consumer, calls = _make_consumer()
    assert await consumer.handle_message("not json") == "ignored_unparseable"
    assert calls == []


@pytest.mark.asyncio
async def test_preflight_force_dry_run(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    consumer, calls = _make_consumer(force_dry_run=True)
    outcome = await consumer.handle_message(_fill_event())
    assert outcome == "reconciled_dry_run"
    assert calls[0]["dry_run"] is True


class _FakePubSub:
    def __init__(self, messages):
        self._messages = messages
        self.unsubscribed = False
        self.closed = False

    async def psubscribe(self, pattern):
        self._pattern = pattern

    async def listen(self):
        for m in self._messages:
            yield m

    async def punsubscribe(self, pattern):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


class _FakeRedisWithPubSub(_FakeRedis):
    def __init__(self, messages):
        super().__init__()
        self._pubsub = _FakePubSub(messages)

    def pubsub(self):
        return self._pubsub


@pytest.mark.asyncio
async def test_run_loop_dispatches_pmessage(monkeypatch):
    monkeypatch.setattr(
        "app.services.kis_mock_execution_consumer.settings."
        "KIS_MOCK_RECONCILE_ON_EXECUTION_ENABLED",
        True,
        raising=False,
    )
    messages = [
        {"type": "psubscribe", "data": 1},  # ignored
        {"type": "pmessage", "channel": "execution:kr", "data": _fill_event()},
    ]
    redis_client = _FakeRedisWithPubSub(messages)
    calls: list[dict] = []

    async def _fake_reconcile(db, *, symbol=None, dry_run=True, **kw):
        calls.append({"symbol": symbol, "dry_run": dry_run})
        return {"success": True}

    consumer = KISMockExecutionConsumer(
        redis_client=redis_client,
        reconcile_fn=_fake_reconcile,
        session_factory=_session_factory,
    )
    await consumer.run()
    assert calls == [{"symbol": "005930", "dry_run": False}]
    assert redis_client._pubsub.unsubscribed is True
