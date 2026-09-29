from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
