import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.toss_symbol_master_service as service_mod
from app.services.brokers.toss.client import TossReadClient
from scripts.sync_toss_symbol_master import _alert_failure, parse_args, run


def test_parse_args_defaults_to_dry_run() -> None:
    args = parse_args(["--market", "kr", "--symbol", "005930"])
    assert args.market == "kr"
    assert args.symbol == ["005930"]
    assert args.commit is False


def test_parse_args_all_excludes_symbol_and_limit() -> None:
    try:
        parse_args(["--market", "kr", "--all", "--symbol", "005930"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser error")


def test_parse_args_timeout_seconds_defaults_to_internal_budget() -> None:
    args = parse_args(["--market", "kr", "--symbol", "005930"])
    assert args.timeout_seconds == 600
    args = parse_args(
        ["--market", "kr", "--symbol", "005930", "--timeout-seconds", "0"]
    )
    assert args.timeout_seconds == 0


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def begin(self) -> _FakeTx:
        return _FakeTx()


class _FakeSessionCM:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeNotifier:
    def __init__(self, delivered: bool = True) -> None:
        self.notify_agent_message = AsyncMock(return_value=delivered)
        self.shutdown = AsyncMock()


def _patch_run_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sync_impl,
    notifier: _FakeNotifier | None,
    notifier_configured: bool = True,
) -> None:
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _FakeSessionCM)
    monkeypatch.setattr(
        TossReadClient,
        "from_settings",
        classmethod(lambda cls: SimpleNamespace(aclose=AsyncMock())),
    )
    monkeypatch.setattr(service_mod, "sync_toss_symbol_master", sync_impl)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.runtime.configure_trade_notifier_from_settings",
        lambda **kwargs: notifier_configured,
    )
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier",
        lambda: notifier,
    )


def _ok_result() -> SimpleNamespace:
    return SimpleNamespace(
        market="kr",
        commit=False,
        symbols_requested=1,
        batches=1,
        stocks_matched=1,
        stocks_missing=0,
        master_updates=0,
        market_cap_payloads=0,
        market_cap_nonnull=0,
        market_cap_skipped_existing=0,
        warnings=(),
        samples=("005930",),
    )


def test_run_alerts_operator_on_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed market sync must produce an operator alert attempt."""
    notifier = _FakeNotifier()
    _patch_run_deps(
        monkeypatch,
        sync_impl=AsyncMock(side_effect=RuntimeError("boom")),
        notifier=notifier,
    )

    args = parse_args(["--market", "kr", "--symbol", "005930"])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run(args))

    notifier.notify_agent_message.assert_awaited_once()
    message = notifier.notify_agent_message.await_args.args[0]
    assert "KR" in message
    assert "RuntimeError" in message
    notifier.shutdown.assert_awaited_once()


def test_run_alerts_operator_on_runtime_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stall must self-abort and alert instead of dying silently at the
    caller's outer kill — this is the 09-22 hang scenario."""

    async def _hang(*args, **kwargs):
        await asyncio.sleep(3600)

    notifier = _FakeNotifier()
    _patch_run_deps(monkeypatch, sync_impl=_hang, notifier=notifier)

    args = parse_args(["--market", "us", "--symbol", "AAPL", "--commit"])
    args.timeout_seconds = 0.05
    with pytest.raises(TimeoutError):
        asyncio.run(run(args))

    notifier.notify_agent_message.assert_awaited_once()
    message = notifier.notify_agent_message.await_args.args[0]
    assert "US" in message
    assert "exceeded runtime budget" in message


def test_run_does_not_alert_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    notifier = _FakeNotifier()
    _patch_run_deps(
        monkeypatch,
        sync_impl=AsyncMock(return_value=_ok_result()),
        notifier=notifier,
    )

    args = parse_args(["--market", "kr", "--symbol", "005930"])
    rc = asyncio.run(run(args))

    assert rc == 0
    notifier.notify_agent_message.assert_not_awaited()


def test_run_warns_loudly_when_no_alert_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing alert config must be an observable warning, not a silent no-op
    (the 09-22 defect: helper returned False and nobody knew)."""
    notifier = _FakeNotifier()
    _patch_run_deps(
        monkeypatch,
        sync_impl=AsyncMock(side_effect=RuntimeError("boom")),
        notifier=notifier,
        notifier_configured=False,
    )

    args = parse_args(["--market", "kr", "--symbol", "005930"])
    with pytest.raises(RuntimeError, match="boom"), caplog.at_level("WARNING"):
        asyncio.run(run(args))

    notifier.notify_agent_message.assert_not_awaited()
    assert any(
        "no operator alert channel is configured" in record.message
        for record in caplog.records
    )


def test_alert_failure_warns_when_send_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = _FakeNotifier(delivered=False)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.runtime.configure_trade_notifier_from_settings",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier",
        lambda: notifier,
    )

    with caplog.at_level("WARNING"):
        asyncio.run(_alert_failure(market="kr", commit=True, reason="RuntimeError"))

    notifier.notify_agent_message.assert_awaited_once()
    notifier.shutdown.assert_awaited_once()
    assert any(
        "operator alert send returned false" in record.message
        for record in caplog.records
    )


def test_alert_failure_never_raises_on_send_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = _FakeNotifier()
    notifier.notify_agent_message = AsyncMock(
        side_effect=ConnectionError("discord unreachable")
    )
    notifier.shutdown = AsyncMock(side_effect=RuntimeError("shutdown failed"))
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.runtime.configure_trade_notifier_from_settings",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier",
        lambda: notifier,
    )

    asyncio.run(_alert_failure(market="us", commit=False, reason="TimeoutError"))

    assert any(
        "operator alert send failed" in record.message for record in caplog.records
    )
