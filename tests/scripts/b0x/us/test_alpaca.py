"""Offline tests for US lab fresh truth, planning, and mutation boundaries."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.b0x.broker_truth import (
    OWN_PENDING_ORDER_EXISTS,
    BrokerTruth,
    OwnPendingResubmitBlocked,
)
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import US_ALPACA_PAPER_LAB_ENVELOPE
from scripts.b0x.table_source import PolicyTable
from scripts.b0x.us import alpaca

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 10, 15, 0, tzinfo=dt.UTC)


def _response(**payload: Any) -> dict[str, Any]:
    return {"success": True, "account_mode": alpaca.LANE, **payload}


def _readers(
    *,
    account: dict[str, Any] | None = None,
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    ledger: list[dict[str, Any]] | None = None,
) -> tuple[alpaca.LabReaders, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def get_account(**kwargs: Any) -> dict[str, Any]:
        calls.append(("account", kwargs))
        return _response(
            account=account
            or {"cash": "5000", "portfolio_value": "5000", "status": "ACTIVE"}
        )

    async def list_positions(**kwargs: Any) -> dict[str, Any]:
        calls.append(("positions", kwargs))
        values = positions or []
        return _response(count=len(values), positions=values)

    async def list_orders(**kwargs: Any) -> dict[str, Any]:
        calls.append(("orders", kwargs))
        values = orders or []
        return _response(count=len(values), orders=values)

    async def list_recent_ledger(**kwargs: Any) -> dict[str, Any]:
        calls.append(("ledger", kwargs))
        values = ledger or []
        return _response(count=len(values), items=values)

    return (
        alpaca.LabReaders(
            get_account=get_account,
            list_positions=list_positions,
            list_orders=list_orders,
            list_recent_ledger=list_recent_ledger,
        ),
        calls,
    )


def _derived(
    *,
    order_key: str,
    symbol: str = "AAPL",
    side: str = "buy",
    price: str = "100",
    notional: str | None = "300",
    fraction: str | None = None,
) -> DerivedOrder:
    return DerivedOrder(
        sequence=0,
        symbol=symbol,
        side=side,
        leg="buy_l1" if side == "buy" else "sell_r1",
        price_ratio=Decimal("0.97") if side == "buy" else Decimal("1.05"),
        table_price=Decimal(price),
        table_previous_close=Decimal("100"),
        notional=Decimal(notional) if notional is not None else None,
        quantity_fraction=Decimal(fraction) if fraction is not None else None,
        basis="fixture",
        labels=(),
        detail={},
        order_key=order_key,
    )


def _table() -> PolicyTable:
    return PolicyTable(
        market="us",
        path=Path("/tmp/latest-us.json"),
        payload={"config": {"new_entry_notional_usd": "300"}, "rows": []},
        policy_table_hash="sha256:test-policy-table",
        artifact_sha256="sha256:test-artifact",
        generated_at=NOW,
        age=dt.timedelta(0),
    )


@pytest.mark.asyncio
async def test_fresh_truth_uses_lab_only_and_attributes_by_b0xu_correlation() -> None:
    readers, calls = _readers(
        positions=[
            {
                "symbol": "AAPL",
                "qty": "2",
                "qty_available": "2",
                "avg_entry_price": "100",
            },
            {
                "symbol": "UBER",
                "qty": "1",
                "qty_available": "1",
                "avg_entry_price": "20",
            },
            {
                "symbol": "BRK-B",
                "qty": "0.1",
                "qty_available": "0.1",
                "avg_entry_price": "400",
            },
        ],
        orders=[
            {"id": "b0xu-open", "symbol": "AAPL"},
            {"id": "foreign-open", "symbol": "UBER"},
        ],
        ledger=[
            {
                "account_mode": alpaca.LANE,
                "record_kind": "execution",
                "lifecycle_correlation_id": "b0xu-aapl-buy",
                "client_order_id": "dlab-rob842a-aapl",
                "broker_order_id": "b0xu-open",
                "execution_symbol": "AAPL",
                "side": "buy",
                "filled_qty": "2",
                "filled_avg_price": "100",
                "created_at": "2026-08-09T15:00:00+00:00",
            }
        ],
    )

    fresh = await alpaca.read_fresh_truth(now=NOW, readers=readers)

    assert {name for name, _ in calls} == {"account", "positions", "orders", "ledger"}
    assert all(kwargs["account_mode"] == alpaca.LANE for _, kwargs in calls)
    orders_call = next(kwargs for name, kwargs in calls if name == "orders")
    assert orders_call == {"status": "open", "limit": 500, "account_mode": alpaca.LANE}
    truth = fresh.broker_truth()
    assert truth.canonical() == {
        "position_symbols": ["AAPL", "BRK.B", "UBER"],
        "own_pending": ["AAPL"],
        "own_pending_readable": True,
    }
    # All three contract v1.5 ① inputs are facts from the same broker snapshot:
    # every positive sellable account position consumes concurrency/daily room,
    # while the linked open order blocks AAPL regardless of its side.
    assert truth.concurrent_position_count == 3
    assert truth.daily_new_entry_seed() == {"AAPL", "BRK.B", "UBER"}
    assert truth.resubmit_block("AAPL")[0] == OWN_PENDING_ORDER_EXISTS
    assert [position.symbol for position in fresh.own_positions] == ["AAPL"]
    assert fresh.foreign_position_symbols == ("BRK.B", "UBER")
    assert [order.broker_order_id for order in fresh.own_open_orders] == ["b0xu-open"]
    assert [order.broker_order_id for order in fresh.foreign_open_orders] == [
        "foreign-open"
    ]
    # No b0xu execution exists on NOW's UTC day, which is a provable bootstrap
    # zero—not an inferred P&L calculation.
    assert fresh.realized_pnl_today == Decimal("0")


@pytest.mark.asyncio
async def test_open_b0xu_order_is_own_pending_before_it_has_any_fill() -> None:
    """A broker-resting B0-X order does not need fabricated fill evidence.

    The submitted execution ledger row supplies the exact broker-order-id ↔
    b0xu correlation.  It is sufficient ownership evidence for the readable
    Alpaca open-orders response, and is intentionally distinct from a fill
    used for position attribution.
    """

    readers, _ = _readers(
        orders=[{"id": "pending-b0xu", "symbol": "AAPL"}],
        ledger=[
            {
                "account_mode": alpaca.LANE,
                "record_kind": "execution",
                "lifecycle_state": "submitted",
                "lifecycle_correlation_id": "b0xu-pending-aapl",
                "client_order_id": "b0xu-client-aapl",
                "broker_order_id": "pending-b0xu",
                "execution_symbol": "AAPL",
                "side": "buy",
                "created_at": "2026-08-09T15:00:00+00:00",
            }
        ],
    )

    fresh = await alpaca.read_fresh_truth(now=NOW, readers=readers)

    assert [order.broker_order_id for order in fresh.own_open_orders] == [
        "pending-b0xu"
    ]
    assert fresh.foreign_open_orders == ()
    assert fresh.broker_truth().resubmit_block("AAPL")[0] == (OWN_PENDING_ORDER_EXISTS)


@pytest.mark.asyncio
async def test_fresh_truth_rejects_account_mode_mismatch_instead_of_falling_back() -> (
    None
):
    readers, _ = _readers()

    async def wrong_account(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["account_mode"] == alpaca.LANE
        return {
            "success": True,
            "account_mode": "alpaca_paper",
            "account": {"cash": "1", "portfolio_value": "1"},
        }

    readers = alpaca.LabReaders(
        get_account=wrong_account,
        list_positions=readers.list_positions,
        list_orders=readers.list_orders,
        list_recent_ledger=readers.list_recent_ledger,
    )
    with pytest.raises(
        alpaca.LabTruthReadError, match="default-account fallback refused"
    ):
        await alpaca.read_fresh_truth(now=NOW, readers=readers)


@pytest.mark.asyncio
async def test_fresh_truth_refuses_truncated_open_order_or_ledger_answers() -> None:
    readers, _ = _readers()

    async def full_orders(**kwargs: Any) -> dict[str, Any]:
        return _response(count=500, orders=[])

    too_many_orders = alpaca.LabReaders(
        get_account=readers.get_account,
        list_positions=readers.list_positions,
        list_orders=full_orders,
        list_recent_ledger=readers.list_recent_ledger,
    )
    with pytest.raises(alpaca.LabTruthReadError, match="open-order read reached"):
        await alpaca.read_fresh_truth(now=NOW, readers=too_many_orders)

    async def full_ledger(**kwargs: Any) -> dict[str, Any]:
        return _response(count=200, items=[])

    too_many_ledger = alpaca.LabReaders(
        get_account=readers.get_account,
        list_positions=readers.list_positions,
        list_orders=readers.list_orders,
        list_recent_ledger=full_ledger,
    )
    with pytest.raises(alpaca.LabTruthReadError, match="ledger read reached"):
        await alpaca.read_fresh_truth(now=NOW, readers=too_many_ledger)


def test_plan_orders_keeps_realized_buys_inside_signed_band_and_symbol_cap() -> None:
    orders = tuple(
        _derived(order_key=f"key-{index}", price="400") for index in range(7)
    )
    planned, blocked = alpaca.plan_orders(
        orders,
        envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
        cash=Decimal("10000"),
        held_quantities={},
        invested_notional_by_symbol={},
        sell_source_client_order_ids={},
    )

    assert [order.notional for order in planned] == [Decimal("400")] * 5
    assert all(
        alpaca.US_NEW_ENTRY_NOTIONAL_MIN
        <= order.notional
        <= US_ALPACA_PAPER_LAB_ENVELOPE.per_order_notional
        for order in planned
    )
    assert sum(order.notional for order in planned) <= Decimal("2250")
    assert len(blocked) == 2
    assert {block.reason for block in blocked} == {"sizing_blocked"}


def test_plan_blocks_sell_without_exact_native_buy_authority() -> None:
    planned, blocked = alpaca.plan_orders(
        (
            _derived(
                order_key="sell",
                side="sell",
                price="110",
                notional=None,
                fraction="0.5",
            ),
        ),
        envelope=US_ALPACA_PAPER_LAB_ENVELOPE,
        cash=Decimal("0"),
        held_quantities={"AAPL": Decimal("2")},
        invested_notional_by_symbol={"AAPL": Decimal("200")},
        sell_source_client_order_ids={},
    )
    assert planned == []
    assert blocked[0].reason == "source_authority_unavailable"


def test_packet_carries_b0xu_correlation_and_lab_only_binding() -> None:
    planned = alpaca.PlannedOrder(
        order_key="packet-key",
        lifecycle_correlation_id="b0xu-packet-key",
        symbol="AAPL",
        side="buy",
        leg="buy_l1",
        price=Decimal("100"),
        quantity=Decimal("3"),
        notional=Decimal("300"),
    )
    packet, canonical = alpaca.build_submission_packet(planned=planned, table=_table())

    assert packet.account_mode == alpaca.LANE
    assert packet.lifecycle_correlation_id.startswith("b0xu-")
    assert packet.signal_venue == "policy_table_us"
    assert packet.max_notional == Decimal("300")
    assert canonical["qty"] == "3"
    assert canonical["notional"] is None


@pytest.mark.asyncio
async def test_submit_rechecks_readable_broker_pending_before_fake_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned = alpaca.PlannedOrder(
        order_key="submit-key",
        lifecycle_correlation_id="b0xu-submit-key",
        symbol="AAPL",
        side="buy",
        leg="buy_l1",
        price=Decimal("100"),
        quantity=Decimal("3"),
        notional=Decimal("300"),
    )
    called: list[dict[str, Any]] = []

    async def fake_submit(packet: Any, **kwargs: Any) -> dict[str, Any]:
        called.append({"packet": packet, **kwargs})
        return {"success": True, "submitted": True, "source": "fake"}

    with pytest.raises(alpaca.UsLabLaneDisabled):
        await alpaca.submit_planned_order(
            planned=planned,
            table=_table(),
            confirm=True,
            broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
            submitter=fake_submit,
        )
    assert called == []

    confirmation_required = await alpaca.submit_planned_order(
        planned=planned,
        table=_table(),
        confirm=False,
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        submitter=fake_submit,
    )
    assert confirmation_required["reason_code"] == "confirmation_required"
    assert called == []
    monkeypatch.setenv(alpaca.US_LANE_ENABLED_ENV, "true")

    result = await alpaca.submit_planned_order(
        planned=planned,
        table=_table(),
        confirm=True,
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        submitter=fake_submit,
    )
    assert result["submitted"] is True
    assert called[0]["packet"].lifecycle_correlation_id == "b0xu-submit-key"
    assert called[0]["confirm"] is True

    with pytest.raises(OwnPendingResubmitBlocked):
        await alpaca.submit_planned_order(
            planned=planned,
            table=_table(),
            confirm=True,
            broker_truth=BrokerTruth(position_symbols=(), own_pending=("AAPL",)),
            submitter=fake_submit,
        )
    assert len(called) == 1


@pytest.mark.asyncio
async def test_production_mutation_defaults_are_unwired_and_fail_closed() -> None:
    planned = alpaca.PlannedOrder(
        order_key="unwired-key",
        lifecycle_correlation_id="b0xu-unwired-key",
        symbol="AAPL",
        side="buy",
        leg="buy_l1",
        price=Decimal("100"),
        quantity=Decimal("3"),
        notional=Decimal("300"),
    )

    with pytest.raises(alpaca.LabMutationNotWired):
        await alpaca.submit_planned_order(
            planned=planned,
            table=_table(),
            confirm=True,
            broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        )

    empty = alpaca.FreshTruth(
        cash=Decimal("1"),
        nav=Decimal("1"),
        positions=(),
        open_orders=(),
        own_open_orders=(),
        foreign_open_orders=(),
        own_positions=(),
        foreign_position_symbols=(),
        position_linkage_failures=(),
        sell_source_client_order_ids={},
        realized_pnl_today=Decimal("0"),
        cumulative_deployment_readable=True,
    )
    with pytest.raises(alpaca.LabMutationNotWired):
        await alpaca.cancel_own_open_orders(fresh=empty, confirm=True)


@pytest.mark.asyncio
async def test_cancel_only_targets_b0xu_linked_open_orders_when_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = alpaca.FreshTruth(
        cash=Decimal("1"),
        nav=Decimal("1"),
        positions=(),
        open_orders=(
            alpaca.RawOpenOrder(broker_order_id="own", symbol="AAPL"),
            alpaca.RawOpenOrder(broker_order_id="foreign", symbol="UBER"),
        ),
        own_open_orders=(alpaca.RawOpenOrder(broker_order_id="own", symbol="AAPL"),),
        foreign_open_orders=(
            alpaca.RawOpenOrder(broker_order_id="foreign", symbol="UBER"),
        ),
        own_positions=(),
        foreign_position_symbols=(),
        position_linkage_failures=(),
        sell_source_client_order_ids={},
        cumulative_deployment_readable=True,
        realized_pnl_today=Decimal("0"),
    )
    calls: list[dict[str, Any]] = []

    async def fake_cancel(order_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"order_id": order_id, **kwargs})
        return {"cancelled_order_id": order_id}

    assert (
        await alpaca.cancel_own_open_orders(
            fresh=fresh, confirm=False, canceler=fake_cancel
        )
        == []
    )
    assert calls == []
    with pytest.raises(alpaca.UsLabLaneDisabled):
        await alpaca.cancel_own_open_orders(
            fresh=fresh, confirm=True, canceler=fake_cancel
        )
    assert calls == []
    monkeypatch.setenv(alpaca.US_LANE_ENABLED_ENV, "true")
    assert await alpaca.cancel_own_open_orders(
        fresh=fresh, confirm=True, canceler=fake_cancel
    ) == [{"cancelled_order_id": "own"}]
    assert calls == [{"order_id": "own", "confirm": True, "account_mode": alpaca.LANE}]
