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


def _daily_rows(**kw):
    base = {"odno": "0000123456", "pdno": "005930", "ord_qty": "1"}
    base.update(kw)
    return [base]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_returns_none_when_no_odno() -> None:
    # No odno in the submit response -> data-precondition, no network call.
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"any": "result"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_returns_fill_when_filled(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=_daily_rows(tot_ccld_qty="1", avg_prvs="70000")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    fill = await broker.confirm_fill({"odno": "0000123456"})
    assert fill == Fill(price=Decimal("70000"), quantity=Decimal("1"))
    # Bounded read-only inquiry: is_mock pinned True, filtered by order number.
    kw = fake_client.domestic_orders.inquire_daily_order_domestic.await_args.kwargs
    assert kw["is_mock"] is True
    assert kw["order_number"] == "0000123456"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_none_when_pending(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        return_value=_daily_rows(tot_ccld_qty="0")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    assert await broker.confirm_fill({"odno": "0000123456"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_none_on_unsupported_mock_api(mocker) -> None:
    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("TR is not available in mock mode.")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    assert await broker.confirm_fill({"odno": "0000123456"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poll_fill_evidence_maps_unsupported_category(mocker) -> None:
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        EvidenceCategory,
        FillVerdict,
    )

    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.domestic_orders.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("VTTC8001R not available in mock")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    ev = await broker._poll_fill_evidence({"odno": "123456"})
    assert ev.verdict is FillVerdict.UNSUPPORTED
    assert ev.category is EvidenceCategory.UNSUPPORTED_MOCK_API


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
