"""Evidence-application rules on in-memory ORM rows (no session, no database).

``_apply_entry`` / ``_apply_exit`` / ``_apply_partial_entry_exposure`` are pure
functions of the rows they mutate, so the lifecycle of a partially filled entry
(including the flat interval after an interim exit) is verified here without a
DB; ``test_state_exec_db.py`` repeats the key paths on the CI database.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.models.binance_trend import BinanceTrendIntent, BinanceTrendSignal
from app.services.brokers.binance.trend.state import (
    TrendOrderEvidence,
    TrendStateBlocked,
    TrendStateService,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 14, 4, tzinfo=dt.UTC)
apply_entry = TrendStateService._apply_entry
apply_exit = TrendStateService._apply_exit
apply_partial = TrendStateService._apply_partial_entry_exposure


def signal(**values) -> BinanceTrendSignal:
    base = {
        "signal_key": "k",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "state": "entry_reserved",
        "entry_qty": D(0),
        "closed_qty": D(0),
        "realized_pnl_usdt": D(0),
        "entry_price": None,
        "entered_at": None,
        "exit_reason": None,
        "exit_at": None,
    }
    base.update(values)
    return BinanceTrendSignal(**base)


def intent(*, reduce_only=False, leg_key="entry", side="BUY", qty="0.6"):
    return BinanceTrendIntent(
        client_order_id="c",
        signal_key="k",
        leg_key=leg_key,
        side=side,
        qty=D(qty),
        reduce_only=reduce_only,
        state="sending",
        executed_qty=D(0),
    )


def evidence(it, *, status="FILLED", executed=None, avg="100"):
    return TrendOrderEvidence(
        client_order_id="c",
        broker_order_id="1",
        symbol="BTCUSDT",
        side=it.side,
        orig_qty=it.qty,
        executed_qty=it.qty if executed is None else D(executed),
        avg_price=D(avg),
        status=status,
        reduce_only=it.reduce_only,
        position_side="BOTH",
    )


def partially_filled_then_stopped(*, entry_working: bool) -> BinanceTrendSignal:
    """Entry 0.6 filled 0.3, then a hard stop sold that 0.3."""
    sig = signal()
    entry = intent()
    apply_partial(
        sig,
        entry,
        evidence(entry, status="PARTIALLY_FILLED", executed="0.3"),
        D("0.3"),
        when=NOW,
    )
    stop = intent(reduce_only=True, leg_key="hard_stop:0", side="SELL", qty="0.3")
    apply_exit(
        sig,
        stop,
        evidence(stop, avg="95"),
        D("0"),
        when=NOW,
        entry_working=entry_working,
    )
    return sig


def test_a_partial_fill_records_exposure_only_when_the_position_confirms_it() -> None:
    sig, entry = signal(), intent()
    ev = evidence(entry, status="PARTIALLY_FILLED", executed="0.3")
    with pytest.raises(TrendStateBlocked, match="position"):
        apply_partial(sig, entry, ev, D("0.2"), when=NOW)
    assert sig.state == "entry_reserved"
    apply_partial(sig, entry, ev, D("0.3"), when=NOW)
    assert (sig.state, sig.entry_qty, sig.entry_price, sig.entered_at) == (
        "holding",
        D("0.3"),
        D("100"),
        NOW,
    )


def test_the_flat_interval_of_a_working_entry_is_not_the_end() -> None:
    sig = partially_filled_then_stopped(entry_working=True)
    assert sig.state == "holding" and sig.entry_qty - sig.closed_qty == 0
    assert (sig.exit_reason, sig.exit_at) == ("hard_stop", NOW)
    assert sig.realized_pnl_usdt == D("-1.5")  # (95 - 100) * 0.3


def test_without_a_working_entry_the_last_exit_closes_the_signal() -> None:
    sig = partially_filled_then_stopped(entry_working=False)
    assert sig.state == "closed" and sig.exit_reason == "hard_stop"


def test_a_later_fill_after_the_interim_exit_restores_a_managed_holding() -> None:
    sig = partially_filled_then_stopped(entry_working=True)
    entry = intent()
    # 0.3 more executed (0.6 in total); the broker now shows 0.3
    apply_partial(
        sig,
        entry,
        evidence(entry, status="PARTIALLY_FILLED", executed="0.6"),
        D("0.3"),
        when=NOW,
    )
    assert sig.state == "holding" and sig.entry_qty - sig.closed_qty == D("0.3")
    entry_filled = evidence(entry, status="FILLED", executed="0.6")
    apply_entry(sig, entry, entry_filled, D("0.3"), when=NOW)
    assert sig.state == "holding" and sig.entry_qty == D("0.6")


def test_a_terminal_entry_without_further_fills_closes_the_flat_signal() -> None:
    sig = partially_filled_then_stopped(entry_working=True)
    entry = intent()
    apply_entry(
        sig, entry, evidence(entry, status="CANCELED", executed="0.3"), D("0"), when=NOW
    )
    assert sig.state == "closed" and sig.exit_reason == "hard_stop"


def test_a_terminal_entry_that_filled_more_is_a_position_again_not_a_close() -> None:
    sig = partially_filled_then_stopped(entry_working=True)
    entry = intent()
    apply_entry(
        sig, entry, evidence(entry, status="FILLED", executed="0.6"), D("0.3"), when=NOW
    )
    assert sig.state == "holding" and sig.entry_qty - sig.closed_qty == D("0.3")


def test_a_terminal_entry_is_checked_against_the_position_and_the_exit_record() -> None:
    sig = partially_filled_then_stopped(entry_working=True)
    entry = intent()
    with pytest.raises(TrendStateBlocked, match="position"):
        apply_entry(
            sig,
            entry,
            evidence(entry, status="CANCELED", executed="0.3"),
            D("0.3"),
            when=NOW,
        )
    sig.exit_reason = None  # a flat signal with no exit record cannot be closed
    with pytest.raises(TrendStateBlocked, match="exit record"):
        apply_entry(
            sig,
            entry,
            evidence(entry, status="CANCELED", executed="0.3"),
            D("0"),
            when=NOW,
        )


def test_a_closed_signal_is_left_alone_by_progress_evidence_and_refuses_new_fills() -> (
    None
):
    sig = partially_filled_then_stopped(entry_working=False)
    entry = intent()
    apply_partial(
        sig,
        entry,
        evidence(entry, status="PARTIALLY_FILLED", executed="0.5"),
        D("0.2"),
        when=NOW,
    )
    assert sig.state == "closed"
    with pytest.raises(TrendStateBlocked, match="awaiting its entry"):
        apply_entry(
            sig,
            entry,
            evidence(entry, status="FILLED", executed="0.6"),
            D("0.3"),
            when=NOW,
        )


def test_a_plain_full_fill_and_full_exit_still_work() -> None:
    sig, entry = signal(), intent()
    apply_entry(sig, entry, evidence(entry), D("0.6"), when=NOW)
    assert (sig.state, sig.entry_qty) == ("holding", D("0.6"))
    stop = intent(reduce_only=True, leg_key="time_exit:0", side="SELL")
    apply_exit(sig, stop, evidence(stop, avg="101"), D("0"), when=NOW)
    assert (sig.state, sig.exit_reason, sig.closed_qty) == (
        "closed",
        "time_exit",
        D("0.6"),
    )
    assert sig.realized_pnl_usdt == D("0.6")


def test_a_short_partial_entry_uses_the_negative_position() -> None:
    sig, entry = signal(side="SELL"), intent(side="SELL")
    apply_partial(
        sig,
        entry,
        evidence(entry, status="PARTIALLY_FILLED", executed="0.3"),
        D("-0.3"),
        when=NOW,
    )
    assert sig.state == "holding"
    cover = intent(reduce_only=True, leg_key="signal_flip:0", side="BUY", qty="0.3")
    apply_exit(
        sig, cover, evidence(cover, avg="98"), D("0"), when=NOW, entry_working=False
    )
    assert sig.state == "closed" and sig.realized_pnl_usdt == D(
        "0.6"
    )  # short gains 2 * 0.3
