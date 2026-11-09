from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.brokers.binance.h5.executor as executor_module
from app.services.brokers.binance.h5.client import H5Filters, H5Quote
from app.services.brokers.binance.h5.executor import H5Executor
from app.services.brokers.binance.h5.strategy import H5Strategy
from tests.services.brokers.binance.h5.fake_state_db import NOW


def test_entry_sizes_from_next_executable_ask_not_historical_open(monkeypatch):
    signal = SimpleNamespace(
        signal_key="s1", symbol="BTCUSDT", side="BUY", decision_ts=1
    )
    state = SimpleNamespace(
        record_nav=AsyncMock(),
        reserve_entry=AsyncMock(return_value=signal),
        reserve_intent=AsyncMock(return_value=SimpleNamespace()),
    )
    client = SimpleNamespace(
        assert_symbol_isolated_1x=AsyncMock(),
        get_position_mode=AsyncMock(return_value=SimpleNamespace(is_hedge_mode=False)),
        read_account=AsyncMock(return_value=SimpleNamespace(nav_usdt=Decimal("1000"))),
        get_book_quote=AsyncMock(
            return_value=H5Quote("BTCUSDT", Decimal("102"), Decimal("103"))
        ),
        get_h5_filters=AsyncMock(
            return_value=H5Filters(
                Decimal("0.1"), Decimal("5"), Decimal("0.1"), Decimal("100"), 1
            )
        ),
    )
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=state,
        demo_ledger=SimpleNamespace(),
        ledger_session=SimpleNamespace(),
    )
    monkeypatch.setattr(executor, "_fresh_exposure", AsyncMock())
    monkeypatch.setattr(executor, "_prepare_demo_entry", AsyncMock())
    monkeypatch.setattr(
        executor,
        "_send_reserved",
        AsyncMock(return_value=SimpleNamespace(state="holding")),
    )
    result = asyncio.run(executor._enter(signal, completed_bar_closes=(), now=NOW))
    assert result.event == "entry_filled"
    assert state.reserve_intent.call_args.kwargs["qty"] == Decimal("1.9")
    assert state.reserve_intent.call_args.kwargs["reduce_only"] is False
    assert state.record_nav.await_count == 1


def test_close_reserves_reduce_only_intent_and_child_ledger_before_send(monkeypatch):
    signal = SimpleNamespace(
        signal_key="s1",
        symbol="BTCUSDT",
        side="BUY",
        closed_qty=Decimal("0.3"),
        entry_client_order_id="h5-entry",
        correlation_id="binance-h5:s1",
    )
    intent = SimpleNamespace(client_order_id="h5-close", reduce_only=True)
    state = SimpleNamespace(reserve_intent=AsyncMock(return_value=intent))
    ledger = SimpleNamespace(
        resolve_or_create_instrument=AsyncMock(return_value=847),
        record_planned=AsyncMock(),
        record_previewed=AsyncMock(),
        record_validated=AsyncMock(),
    )
    executor = H5Executor(
        client=SimpleNamespace(assert_symbol_isolated_1x=AsyncMock()),
        strategy=H5Strategy(),
        state=state,
        demo_ledger=ledger,
        ledger_session=SimpleNamespace(commit=AsyncMock()),
    )
    monkeypatch.setattr(executor, "_fresh_exposure", AsyncMock())
    sent = AsyncMock(return_value=signal)
    monkeypatch.setattr(executor, "_send_reserved", sent)
    asyncio.run(executor._close(signal, reason="tp1", qty=Decimal("0.2"), now=NOW))
    args = state.reserve_intent.call_args.kwargs
    assert (
        args["qty"] == Decimal("0.2")
        and args["side"] == "SELL"
        and args["reduce_only"] is True
    )
    assert (
        ledger.record_planned.call_args.kwargs["parent_client_order_id"] == "h5-entry"
    )
    assert (
        ledger.record_planned.call_args.kwargs["extra_metadata"]["reduce_only"] is True
    )
    assert sent.call_args.kwargs["intent"].reduce_only is True


# --- manage-only tick (hk 3644) ---
class _TrapClient:
    """Only the NAV read is allowed; any other broker call fails the test."""

    def __init__(self) -> None:
        self.read_account = AsyncMock(
            return_value=SimpleNamespace(nav_usdt=Decimal("1000"))
        )

    def __getattr__(self, name: str):
        raise AssertionError(f"broker call {name!r} is not allowed")


def _state(**overrides):
    state = SimpleNamespace(
        record_nav=AsyncMock(),
        list_unresolved_intents=AsyncMock(return_value=()),
        list_active_signals=AsyncMock(return_value=()),
        list_forecast_recovery_signals=AsyncMock(return_value=()),
        decision_processed=AsyncMock(return_value=False),
        record_opportunity_grid=AsyncMock(),
        observe_signal=AsyncMock(),
        reserve_entry=AsyncMock(),
        reserve_intent=AsyncMock(),
        block_unsent=AsyncMock(),
        mark_decision_processed=AsyncMock(),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _executor(client, state, ledger=None, session=None) -> H5Executor:
    """The identity guard is covered elsewhere; the trap client is not a real one."""
    executor = H5Executor(
        client=client,
        strategy=H5Strategy(),
        state=state,
        demo_ledger=ledger or SimpleNamespace(),
        ledger_session=session or SimpleNamespace(),
    )
    executor._guard = lambda **kwargs: None  # type: ignore[method-assign]
    return executor


ENTRY_PATH_STATE_CALLS = (
    "decision_processed",
    "record_opportunity_grid",
    "observe_signal",
    "reserve_entry",
    "reserve_intent",
    "block_unsent",
    "mark_decision_processed",
)


def test_manage_only_never_reaches_signal_collection_or_entry(monkeypatch):
    collect = AsyncMock(side_effect=AssertionError("signal collection reached"))
    monkeypatch.setattr(executor_module, "collect_signal_history", collect)
    submit = AsyncMock(side_effect=AssertionError("an order was sent"))
    state, client = _state(), _TrapClient()
    client.submit_order = submit  # type: ignore[attr-defined]
    executor = _executor(client, state)
    result = asyncio.run(executor.run_tick(now=NOW, confirm=True, manage_only=True))
    assert result.event == "entries_skipped_manage_only"
    assert result.signal_keys == ()
    assert collect.await_count == 0 and submit.await_count == 0
    for name in ENTRY_PATH_STATE_CALLS:
        assert getattr(state, name).await_count == 0, name
    assert state.record_nav.await_count == 1  # NAV/heartbeat still runs


def test_without_the_flag_the_same_tick_reaches_signal_collection(monkeypatch):
    """Control for the test above: the trap is live on the default path."""
    collect = AsyncMock(side_effect=AssertionError("signal collection reached"))
    monkeypatch.setattr(executor_module, "collect_signal_history", collect)
    executor = _executor(_TrapClient(), _state())
    with pytest.raises(AssertionError, match="signal collection reached"):
        asyncio.run(executor.run_tick(now=NOW, confirm=True))


def test_manage_only_still_blocks_on_uncertain_exposure():
    state = _state(
        list_active_signals=AsyncMock(
            return_value=(SimpleNamespace(state="uncertain"),)
        )
    )
    result = asyncio.run(
        _executor(_TrapClient(), state).run_tick(
            now=NOW, confirm=True, manage_only=True
        )
    )
    assert result.event == "blocked" and result.detail == "uncertain H5 exposure"


def _holding_tick(monkeypatch, *, manage_only: bool):
    """One tick over a long held past 24h: the time exit must close all of it."""
    entered = NOW - dt.timedelta(hours=30)
    signal = SimpleNamespace(
        signal_key="s1",
        symbol="BTCUSDT",
        side="BUY",
        state="holding",
        entered_at=entered,
        entry_price=Decimal("100"),
        entry_qty=Decimal("1"),
        closed_qty=Decimal("0"),
        entry_client_order_id="h5-entry",
        correlation_id="binance-h5:s1",
    )
    intent = SimpleNamespace(client_order_id="h5-close", reduce_only=True)
    state = _state(
        list_active_signals=AsyncMock(return_value=(signal,)),
        reserve_intent=AsyncMock(return_value=intent),
    )
    ledger = SimpleNamespace(
        resolve_or_create_instrument=AsyncMock(return_value=847),
        record_planned=AsyncMock(),
        record_previewed=AsyncMock(),
        record_validated=AsyncMock(),
    )
    client = _TrapClient()
    client.get_book_quote = AsyncMock(  # type: ignore[attr-defined]
        return_value=H5Quote("BTCUSDT", Decimal("100"), Decimal("101"))
    )
    client.get_h5_filters = AsyncMock(  # type: ignore[attr-defined]
        return_value=H5Filters(
            Decimal("0.1"), Decimal("5"), Decimal("0.1"), Decimal("100"), 1
        )
    )
    client.assert_symbol_isolated_1x = AsyncMock()  # type: ignore[attr-defined]
    executor = _executor(client, state, ledger, SimpleNamespace(commit=AsyncMock()))
    monkeypatch.setattr(
        executor,
        "_fresh_exposure",
        AsyncMock(
            return_value=[SimpleNamespace(symbol="BTCUSDT", position_amt=Decimal("1"))]
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "collect_holding_history",
        AsyncMock(return_value=([], [])),
    )
    sent = AsyncMock(return_value=signal)
    monkeypatch.setattr(executor, "_send_reserved", sent)
    result = asyncio.run(
        executor.run_tick(now=NOW, confirm=True, manage_only=manage_only)
    )
    return result, state, ledger, sent


def test_manage_only_sends_the_same_reduce_only_close_as_a_normal_tick(monkeypatch):
    plain = _holding_tick(monkeypatch, manage_only=False)
    managed = _holding_tick(monkeypatch, manage_only=True)
    for result, state, ledger, sent in (plain, managed):
        assert result.event == "close_sent" and result.detail == "time_exit"
        args = state.reserve_intent.call_args.kwargs
        assert args["side"] == "SELL" and args["reduce_only"] is True
        assert args["qty"] == Decimal("1")
        assert (
            ledger.record_planned.call_args.kwargs["extra_metadata"]["reduce_only"]
            is True
        )
        assert sent.call_args.kwargs["intent"].reduce_only is True
        assert state.decision_processed.await_count == 0  # closed, tick ended
    assert managed[1].reserve_intent.call_args == plain[1].reserve_intent.call_args
    assert managed[2].record_planned.call_args == plain[2].record_planned.call_args
