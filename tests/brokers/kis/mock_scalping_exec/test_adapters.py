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
    mocker.patch.object(
        broker, "_read_snapshot", new=AsyncMock(return_value=(Decimal("0"), None))
    )
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
    reserve = mocker.patch.object(mod, "reserve_entry", new=AsyncMock())
    release = mocker.patch.object(mod, "release_entry", new=AsyncMock())
    broker = KisMockBroker(get_state=lambda s: None)
    mocker.patch.object(
        broker, "_read_snapshot", new=AsyncMock(return_value=(Decimal("1"), None))
    )
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
    reserve.assert_awaited_once_with(
        correlation_id="cid1", symbol="005930", side="sell"
    )
    release.assert_awaited_once_with(correlation_id="cid1", side="sell")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_market_state_uses_mock_orderbook_for_existing_guard(
    mocker,
) -> None:
    """A non-WebSocket caller gets a real mock-host book, not a synthetic quote."""

    broker = KisMockBroker(get_state=lambda _s: None, clock=lambda: 100.0)
    client = mocker.MagicMock()
    client.inquire_orderbook = AsyncMock(
        return_value={
            "bidp1": "70000",
            "askp1": "70100",
            "bidp_rsqn1": "12",
            "askp_rsqn1": "10",
            "stck_prpr": "70050",
        }
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=client)

    state = await broker.refresh_market_state(symbol="005930")

    client.inquire_orderbook.assert_awaited_once_with("005930", "J")
    assert state.bid == 70000.0
    assert state.ask == 70100.0
    assert broker.quote("005930") == Quote(
        bid=Decimal("70000.0"), ask=Decimal("70100.0"), last=Decimal("70050.0")
    )
    await broker._make_pre_send_hook("005930")()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_market_state_rejects_malformed_orderbook(mocker) -> None:
    broker = KisMockBroker(get_state=lambda _s: None)
    client = mocker.MagicMock()
    client.inquire_orderbook = AsyncMock(return_value={"bidp1": "0", "askp1": ""})
    mocker.patch.object(broker, "_get_mock_client", return_value=client)

    with pytest.raises(mod.PreSendFreshnessError) as exc_info:
        await broker.refresh_market_state(symbol="005930")

    assert exc_info.value.reason_codes == ("invalid_orderbook",)
    assert broker.quote("005930") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_holding_qty_makes_confirm_fill_fail_closed(mocker) -> None:
    """A malformed baseline must remain unknown, never become a zero holding."""

    broker = KisMockBroker(get_state=lambda _s: None)
    client = mocker.MagicMock()
    client.fetch_domestic_balance_snapshot = AsyncMock(
        side_effect=[
            {
                "holdings": [{"pdno": "005930", "hldg_qty": "N/A"}],
                "cash": {"dnca_tot_amt": "1000000"},
            },
            {
                "holdings": [{"pdno": "005930", "hldg_qty": "1"}],
                "cash": {"dnca_tot_amt": "930000"},
            },
        ]
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=client)

    baseline = await broker._capture_baseline(
        symbol="005930",
        side="buy",
        qty=Decimal("1"),
        limit_price=Decimal("70000"),
    )

    assert baseline["holdings_qty"] is None
    assert await broker.confirm_fill({"_baseline": baseline}) is None
    client.fetch_domestic_balance_snapshot.assert_awaited_once_with(is_mock=True)


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
async def test_confirm_fill_returns_none_when_no_baseline() -> None:
    # ROB-341: confirm_fill now needs the baseline snapshot stamped at submit
    # time. No baseline -> fail closed, no network call.
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"odno": "0000123456"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_uses_holdings_delta_not_daily_ccld(mocker) -> None:
    # ROB-341: a filled buy is proven by the holdings delta (0 -> 1), with the
    # fill price derived from the cash delta. daily-ccld is NOT consulted.
    broker = KisMockBroker(get_state=lambda s: None)
    mocker.patch.object(
        broker,
        "_read_snapshot",
        new=AsyncMock(return_value=(Decimal("1"), Decimal("930000"))),
    )
    submit_result = {
        "odno": "0000123456",
        "_baseline": {
            "symbol": "005930",
            "side": "buy",
            "ordered_qty": "1",
            "limit_price": "70000",
            "holdings_qty": "0",
            "cash": "1000000",
        },
    }
    fill = await broker.confirm_fill(submit_result)
    assert fill == Fill(price=Decimal("70000"), quantity=Decimal("1"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poll_daily_ccld_diagnostic_maps_unsupported_category(mocker) -> None:
    # ROB-341: daily-ccld is retained only as a NON-GATING supplementary /
    # post-settlement diagnostic. Its classification wiring stays covered.
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
        EvidenceCategory,
        FillVerdict,
    )

    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(
        side_effect=RuntimeError("VTTC8001R not available in mock")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    ev = await broker.poll_daily_ccld_diagnostic({"odno": "123456"})
    assert ev.verdict is FillVerdict.UNSUPPORTED
    assert ev.category is EvidenceCategory.UNSUPPORTED_MOCK_API


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poll_daily_ccld_diagnostic_classifies_filled_rows(mocker) -> None:
    # daily-ccld still parses a populated same-day row when present (post-
    # settlement evidence) — but it does not gate confirm_fill.
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    broker = KisMockBroker(get_state=lambda s: None)
    fake_client = mocker.MagicMock()
    fake_client.inquire_daily_order_domestic = AsyncMock(
        return_value=_daily_rows(tot_ccld_qty="1", avg_prvs="70000")
    )
    mocker.patch.object(broker, "_get_mock_client", return_value=fake_client)
    ev = await broker.poll_daily_ccld_diagnostic({"odno": "0000123456"})
    assert ev.verdict is FillVerdict.FILLED
    assert ev.avg_price == Decimal("70000")


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
