from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.binance.h5 import forecast
from app.services.brokers.binance.h5.executor import H5Executor
from app.services.brokers.binance.h5.state import h5_correlation_id
from app.services.brokers.binance.h5.strategy import H5Strategy
from scripts.binance_h5_demo import _guard_cli


class _Session:
    commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def test_forecast_saves_h5_correlation_and_resolves_actual_held_outcome(monkeypatch):
    now = dt.datetime(2026, 9, 28, 4, tzinfo=dt.UTC)
    saved, resolved = AsyncMock(), AsyncMock(return_value={"status": "resolved"})
    monkeypatch.setattr(forecast, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(forecast, "save_forecast", saved)
    monkeypatch.setattr(forecast, "resolve_forecast", resolved)
    signal = SimpleNamespace(
        signal_key="BTCUSDT|2026-09-28 13:00|BUY|100.00",
        symbol="BTCUSDT",
        side="BUY",
        entry_price=Decimal("100"),
        entry_qty=Decimal("1"),
        correlation_id=h5_correlation_id("s1"),
        state="holding",
    )
    state = SimpleNamespace(mark_forecast_id=AsyncMock())
    fid = asyncio.run(forecast.ensure_entry_forecast(signal, state, now=now))
    target = saved.call_args.kwargs["forecast_target"]
    assert target["kind"] == "h5_held_outcome"
    assert target["entry_qty"] == "1" and target["signal_key"] == signal.signal_key
    assert saved.call_args.kwargs["correlation_id"].startswith("binance-h5:")
    signal.state, signal.forecast_id = "closed", fid
    signal.realized_pnl_usdt, signal.fees_usdt = Decimal("5"), Decimal("0.1")
    signal.closed_qty, signal.entry_client_order_id = Decimal("1"), "h5-entry"
    signal.exit_reason, signal.exit_at = "tp2", now
    asyncio.run(forecast.resolve_held_outcome(signal, now=now))
    assert resolved.call_args.kwargs["manual_outcome"] is True
    assert resolved.call_args.kwargs["manual_observed_value"] == 4.9
    assert resolved.call_args.kwargs["backfill_missing"] is False


@pytest.mark.parametrize(
    "h5,futures,confirm",
    [(None, "true", True), ("true", None, True), ("true", "true", False)],
)
def test_default_off_and_per_call_confirm_block_cli(monkeypatch, h5, futures, confirm):
    monkeypatch.delenv("BINANCE_H5_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    if h5 is not None:
        monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", h5)
    if futures is not None:
        monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", futures)
    with pytest.raises(SystemExit):
        _guard_cli(SimpleNamespace(confirm_demo=confirm))


def test_restart_forecast_failure_does_not_preempt_protective_exit(monkeypatch):
    now = dt.datetime(2026, 9, 28, 4, tzinfo=dt.UTC)
    state = SimpleNamespace(
        record_nav=AsyncMock(),
        list_unresolved_intents=AsyncMock(return_value=()),
        list_active_signals=AsyncMock(return_value=(SimpleNamespace(state="holding"),)),
        list_forecast_recovery_signals=AsyncMock(
            side_effect=AssertionError("must manage holding first")
        ),
    )
    executor = H5Executor(
        client=SimpleNamespace(
            read_account=AsyncMock(
                return_value=SimpleNamespace(nav_usdt=Decimal("1000"))
            )
        ),
        strategy=H5Strategy(),
        state=state,
        demo_ledger=SimpleNamespace(),
        ledger_session=SimpleNamespace(),
    )
    monkeypatch.setattr(executor, "_guard", lambda **kwargs: None)
    executor._manage_holding = AsyncMock(return_value="protective-close")
    assert asyncio.run(executor.run_tick(now=now, confirm=True)) == "protective-close"
    assert state.list_forecast_recovery_signals.await_count == 0
