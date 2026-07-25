"""ROB-993 adversarial-review regression tests (verify-993-2256.md, Findings
2/3/4) — pre-submit broker-flat gate, own-fill-attributed close qty, root
exposure slot held blocking until reconcile completes, and reduceOnly
broker-echo verification. Uses controllable fakes (``_fakes.py``), never
real HTTP/DB, so a deliberately mutated/tampered broker response can be
reproduced deterministically."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.binance_demo_order_ledger import BLOCKING_ROOT_LIFECYCLE_STATES
from app.services.brokers.binance.demo_strategy_loop.execution import (
    BrokerEchoMismatch,
    RoundTripBlocked,
    execute_signal_round_trip,
)
from app.services.brokers.binance.demo_strategy_loop.strategy import Signal
from app.services.brokers.binance.futures_demo.dto import FuturesDemoOpenOrder

from ._fakes import FakeExecutionClient, FakeLedger, FakeSession

_SIGNAL = Signal(
    symbol="XRPUSDT",
    side="BUY",
    decision_ts=1_700_000_000_000,
    strategy_id="test-strategy",
    reason="unit test",
)


def _round_trip_kwargs(execution, ledger, session, **overrides):
    kwargs = {
        "execution": execution,
        "ledger": ledger,
        "session": session,
        "signal": _SIGNAL,
        "instrument_id": 1,
        "venue_host": "demo-fapi.binance.com",
        "qty": Decimal("9.1"),
        "notional_usdt": Decimal("10"),
        "leverage": 1,
        "strategy_loop_tag": "rob-993-strategy-loop",
        "correlation_id": "test-correlation",
        "open_client_order_id": "open-1",
        "close_client_order_id": "close-1",
        "close_step_size": Decimal("0.1"),
        "close_quantity_precision": 1,
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Finding 2 — pre-submit broker-flat gate + own-fill-attributed close qty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_open_submit_when_broker_position_not_flat_pre_submit() -> None:
    """A pre-existing position on the shared Demo account (e.g. from another
    consumer of the same credentials) must block the open submit entirely —
    not get silently added to / closed alongside."""
    execution = FakeExecutionClient(position_amt_by_symbol={"XRPUSDT": Decimal("5")})
    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RoundTripBlocked):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert "submit_order" not in execution.call_names()
    assert "get_all_positions" in execution.call_names()
    assert ledger.rows["open-1"].lifecycle_state not in BLOCKING_ROOT_LIFECYCLE_STATES


@pytest.mark.asyncio
async def test_refuses_open_submit_when_broker_has_residual_open_orders_pre_submit() -> (
    None
):
    execution = FakeExecutionClient()
    execution.open_orders_by_symbol["XRPUSDT"] = [
        FuturesDemoOpenOrder(
            client_order_id="stray",
            broker_order_id="b1",
            symbol="XRPUSDT",
            side="SELL",
            qty=Decimal("1"),
            status="NEW",
            reduce_only=False,
        )
    ]
    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RoundTripBlocked):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert "submit_order" not in execution.call_names()


@pytest.mark.asyncio
async def test_refuses_open_submit_when_a_different_symbol_has_existing_position() -> (
    None
):
    """ROB-993 R2 adversarial review (verify-993-r2-2329.md, Finding 2): the
    broker-flat gate must be account-wide, not scoped to the signal's own
    symbol — a position on a DIFFERENT symbol (left by another consumer of
    the shared Demo account) must also block a new open, since the
    max-concurrent-positions=1 invariant is account-global."""
    execution = FakeExecutionClient(position_amt_by_symbol={"DOGEUSDT": Decimal("5")})
    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RoundTripBlocked):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert "submit_order" not in execution.call_names()
    assert "get_all_positions" in execution.call_names()


@pytest.mark.asyncio
async def test_refuses_open_submit_when_a_different_symbol_has_residual_open_order() -> (
    None
):
    execution = FakeExecutionClient()
    execution.open_orders_by_symbol["DOGEUSDT"] = [
        FuturesDemoOpenOrder(
            client_order_id="stray-doge",
            broker_order_id="b1",
            symbol="DOGEUSDT",
            side="SELL",
            qty=Decimal("1"),
            status="NEW",
            reduce_only=False,
        )
    ]
    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RoundTripBlocked):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert "submit_order" not in execution.call_names()


@pytest.mark.asyncio
async def test_refuses_open_submit_when_position_appears_between_order_test_and_submit() -> (
    None
):
    """ROB-993 R2 adversarial review Finding 2: the broker-flat gate must be
    re-checked fresh immediately before the mutating open submit, not just
    once right after reservation — a position/order that appears in the
    gap (order-test, position-mode, leverage, preview all happen in
    between) must still be caught before any broker mutation."""
    execution = FakeExecutionClient()
    real_order_test = execution.order_test

    async def _order_test_then_inject_position(**kwargs):
        result = await real_order_test(**kwargs)
        execution.position_amt_by_symbol["XRPUSDT"] = Decimal("1")
        return result

    execution.order_test = _order_test_then_inject_position  # type: ignore[method-assign]

    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RoundTripBlocked):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert "submit_order" not in execution.call_names()


@pytest.mark.asyncio
async def test_own_fill_delta_mismatch_aborts_before_close() -> None:
    """If the account's position delta after our own open submit doesn't
    match what we submitted (e.g. a concurrent process traded the same
    symbol in the narrow window), the close must not blindly flatten
    whatever the account currently holds."""
    execution = FakeExecutionClient()

    # Make our own submit_order silently move the position by MORE than we
    # asked for, simulating interference from another consumer of the same
    # shared Demo account between our pre-submit check and our own fill.
    real_submit_order = execution.submit_order

    async def _interfered_submit_order(**kwargs):
        result = await real_submit_order(**kwargs)
        execution.position_amt_by_symbol["XRPUSDT"] += Decimal("3")
        return result

    execution.submit_order = _interfered_submit_order  # type: ignore[method-assign]

    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(RuntimeError):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    # The close leg must never have been planned/submitted against an
    # unverified quantity.
    assert "close-1" not in ledger.rows or ledger.rows["close-1"].lifecycle_state in (
        "planned",
    )
    close_submits = [
        c
        for name, c in execution.calls
        if name == "submit_order" and c.get("reduce_only")
    ]
    assert close_submits == []


# ---------------------------------------------------------------------------
# Finding 3 — root exposure slot held blocking until reconcile completes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_never_transitions_to_closed_before_reconcile_checks_run() -> None:
    """The open root's ``closed`` transition must be written only AFTER the
    final open_orders/position-flat reconcile checks have already run —
    never before, which would release the exposure slot (closed is not a
    ``BLOCKING_ROOT_LIFECYCLE_STATES`` member) while the broker state is
    still unverified."""
    events: list[tuple[str, ...]] = []
    execution = FakeExecutionClient(event_log=events)
    ledger = FakeLedger(event_log=events)
    session = FakeSession()

    result = await execute_signal_round_trip(
        **_round_trip_kwargs(execution, ledger, session)
    )
    assert result.reconciled is True

    closed_idx = events.index(("ledger", "open-1", "closed"))
    last_reconcile_check_idx = max(
        i
        for i, e in enumerate(events)
        if e[0] == "exec" and e[1] in ("get_open_orders", "get_position")
    )
    assert closed_idx > last_reconcile_check_idx, (
        f"root written 'closed' before the final broker reconcile check ran: {events}"
    )


@pytest.mark.asyncio
async def test_root_stays_in_blocking_state_when_reconcile_finds_residual_orders() -> (
    None
):
    """If close-side reconcile finds a residual open order, the root must
    never have passed through the non-blocking ``closed`` state."""
    events: list[tuple[str, ...]] = []
    execution = FakeExecutionClient(event_log=events)
    ledger = FakeLedger(event_log=events)
    session = FakeSession()

    # Inject the residual order only once the close submit has happened, by
    # wrapping submit_order.
    real_submit_order = execution.submit_order

    async def _submit_then_leave_residual(**kwargs):
        result = await real_submit_order(**kwargs)
        if kwargs.get("reduce_only"):
            execution.open_orders_by_symbol["XRPUSDT"] = [
                FuturesDemoOpenOrder(
                    client_order_id="stray-after-close",
                    broker_order_id="b2",
                    symbol="XRPUSDT",
                    side="SELL",
                    qty=Decimal("1"),
                    status="NEW",
                    reduce_only=False,
                )
            ]
        return result

    execution.submit_order = _submit_then_leave_residual  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    recorded_states_for_open = [
        e[2] for e in events if e[0] == "ledger" and e[1] == "open-1"
    ]
    assert "closed" not in recorded_states_for_open
    assert ledger.rows["open-1"].lifecycle_state == "anomaly"


# ---------------------------------------------------------------------------
# Finding 4 — reduceOnly / order-shape broker-echo verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_submit_reduce_only_echo_mismatch_is_rejected() -> None:
    """A close submit response that echoes ``reduceOnly=false`` for a
    request that asked for ``reduceOnly=true`` must be treated as broker
    tampering/anomaly, never a clean success."""
    execution = FakeExecutionClient()
    execution.submit_mutations = {"reduce_only": False}
    ledger = FakeLedger()
    session = FakeSession()

    with pytest.raises(BrokerEchoMismatch):
        await execute_signal_round_trip(
            **_round_trip_kwargs(execution, ledger, session)
        )

    assert ledger.rows["close-1"].lifecycle_state == "anomaly"
    assert ledger.rows["open-1"].lifecycle_state == "anomaly"
