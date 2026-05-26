"""Broker/ledger adapter tests (ROB-321 PR4b)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis.mock_scalping_exec import adapters as mod
from app.services.brokers.kis.mock_scalping_exec.adapters import (
    KisMockBroker,
    KisMockLedgerWriter,
)
from app.services.brokers.kis.mock_scalping_exec.executor import Fill, Quote
from app.services.brokers.kis.mock_scalping_ws.state import MarketState


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_buy_dry_run_calls_place_order_impl(mocker) -> None:
    place = mocker.patch.object(
        mod, "_place_order_impl", new=AsyncMock(return_value={})
    )
    broker = KisMockBroker(get_state=lambda s: None)
    await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id="cid1",
        confirm=False,
    )
    kw = place.await_args.kwargs
    assert kw["side"] == "buy"
    assert kw["is_mock"] is True
    assert kw["dry_run"] is True  # confirm=False -> dry-run preview


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_exit_sell_uses_scalping_exit(mocker) -> None:
    place = mocker.patch.object(
        mod, "_place_order_impl", new=AsyncMock(return_value={})
    )
    broker = KisMockBroker(get_state=lambda s: None)
    await broker.submit_exit_sell(
        symbol="005930",
        price=Decimal("69800"),
        quantity=Decimal("1"),
        exit_reason="stop_loss",
        strategy_id="kis-mock-v1",
        correlation_id="cid1",
        confirm=True,
    )
    kw = place.await_args.kwargs
    assert kw["side"] == "sell"
    assert kw["is_mock"] is True
    assert kw["dry_run"] is False
    assert kw["scalping_exit"] is True
    assert kw["scalping_exit_reason"] == "stop_loss"
    assert kw["scalping_strategy_id"] == "kis-mock-v1"


@pytest.mark.unit
def test_quote_maps_market_state_to_decimal() -> None:
    state = MarketState(symbol="005930")
    state.bid, state.ask, state.last_price = 70000.0, 70100.0, 70050.0
    broker = KisMockBroker(get_state=lambda s: state)
    q = broker.quote("005930")
    assert q == Quote(
        bid=Decimal("70000.0"), ask=Decimal("70100.0"), last=Decimal("70050.0")
    )


@pytest.mark.unit
def test_quote_none_when_no_state() -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    assert broker.quote("005930") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_returns_none_pending_validation() -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"any": "result"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ledger_record_entry_writes_entry_role(mocker) -> None:
    save = mocker.patch.object(
        mod, "_save_kis_mock_order_ledger", new=AsyncMock(return_value=1)
    )
    writer = KisMockLedgerWriter()
    await writer.record_entry(
        correlation_id="cid1",
        symbol="005930",
        strategy_id="kis-mock-v1",
        fill=Fill(Decimal("70000"), Decimal("1")),
    )
    kw = save.await_args.kwargs
    assert kw["scalping_role"] == "entry"
    assert kw["correlation_id"] == "cid1"
    assert kw["lifecycle_state"] == "fill"
    assert kw["side"] == "buy"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ledger_record_exit_reconciled_writes_pnl(mocker) -> None:
    save = mocker.patch.object(
        mod, "_save_kis_mock_order_ledger", new=AsyncMock(return_value=1)
    )
    writer = KisMockLedgerWriter()
    await writer.record_exit_reconciled(
        correlation_id="cid1",
        symbol="005930",
        exit_reason="take_profit",
        entry_fill=Fill(Decimal("70000"), Decimal("1")),
        exit_fill=Fill(Decimal("70300"), Decimal("1")),
        gross_pnl=Decimal("300"),
        net_pnl=Decimal("277"),
        fees=Decimal("23"),
    )
    kw = save.await_args.kwargs
    assert kw["scalping_role"] == "exit"
    assert kw["lifecycle_state"] == "reconciled"
    assert kw["exit_reason"] == "take_profit"
    assert kw["gross_pnl"] == Decimal("300")
    assert kw["net_pnl"] == Decimal("277")
