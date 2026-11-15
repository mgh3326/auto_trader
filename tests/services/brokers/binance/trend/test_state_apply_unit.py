"""Evidence-application rules on in-memory ORM rows (no session, no database).

``_apply_entry`` / ``_apply_exit`` are pure functions of the rows they mutate.
Design decision B (PR #2432 round 7): a still-working partially filled entry is
never recorded as exposure; only terminal evidence turns executed quantity into
a holding, so entry and exit fills never run as two streams on one position.
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


def signal(**values) -> BinanceTrendSignal:
    base = {
        "signal_key": "k",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "state": "entry_reserved",
        "entry_qty": D(0),
        "closed_qty": D(0),
        "realized_pnl_usdt": D(0),
        "fees_usdt": D(0),
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


def test_a_full_fill_and_a_full_exit_close_the_round_trip() -> None:
    sig, entry = signal(), intent()
    apply_entry(sig, entry, evidence(entry), D("0.6"), when=NOW)
    assert (sig.state, sig.entry_qty, sig.entry_price) == (
        "holding",
        D("0.6"),
        D("100"),
    )
    stop = intent(reduce_only=True, leg_key="time_exit:0", side="SELL")
    apply_exit(sig, stop, evidence(stop, avg="101"), D("0"), when=NOW)
    assert (sig.state, sig.exit_reason, sig.closed_qty) == (
        "closed",
        "time_exit",
        D("0.6"),
    )
    assert sig.realized_pnl_usdt == D("0.6")
    # Estimated taker fees accrue on both proven fills: 0.6*100*5e-4 +
    # 0.6*101*5e-4 — the forecast outcome is net of this, not gross.
    assert sig.fees_usdt == D("0.0603")


def test_a_terminal_partial_entry_holds_exactly_the_executed_quantity() -> None:
    sig, entry = signal(), intent()
    ev = evidence(entry, status="CANCELED", executed="0.3")
    with pytest.raises(TrendStateBlocked, match="position"):
        apply_entry(sig, entry, ev, D("0.6"), when=NOW)
    assert sig.state == "entry_reserved"
    apply_entry(sig, entry, ev, D("0.3"), when=NOW)
    assert (sig.state, sig.entry_qty) == ("holding", D("0.3"))


def test_a_terminal_entry_with_nothing_executed_blocks_the_signal() -> None:
    sig, entry = signal(), intent()
    apply_entry(
        sig, entry, evidence(entry, status="EXPIRED", executed="0"), D("0"), when=NOW
    )
    assert sig.state == "blocked"


@pytest.mark.parametrize("state", ["holding", "closed", "blocked", "observed"])
def test_entry_evidence_is_refused_for_a_signal_that_is_not_awaiting_it(state) -> None:
    sig, entry = signal(state=state), intent()
    with pytest.raises(TrendStateBlocked, match="awaiting its entry"):
        apply_entry(sig, entry, evidence(entry), D("0.6"), when=NOW)


def test_a_partial_exit_leaves_the_remainder_held_and_a_wrong_position_raises() -> None:
    sig, entry = signal(), intent()
    apply_entry(sig, entry, evidence(entry), D("0.6"), when=NOW)
    leg = intent(reduce_only=True, leg_key="signal_flip:0", side="SELL", qty="0.5")
    with pytest.raises(TrendStateBlocked, match="position"):
        apply_exit(sig, leg, evidence(leg), D("0"), when=NOW)
    apply_exit(sig, leg, evidence(leg), D("0.1"), when=NOW)
    assert sig.state == "holding" and sig.closed_qty == D("0.5")
    assert sig.exit_reason is None


def test_a_short_round_trip_uses_negative_positions_and_pnl_direction() -> None:
    sig, entry = signal(side="SELL"), intent(side="SELL")
    apply_entry(sig, entry, evidence(entry), D("-0.6"), when=NOW)
    cover = intent(reduce_only=True, leg_key="hard_stop:0", side="BUY")
    apply_exit(sig, cover, evidence(cover, avg="98"), D("0"), when=NOW)
    assert sig.state == "closed" and sig.realized_pnl_usdt == D("1.2")
    assert sig.fees_usdt == D("0.0594")  # 0.6*100*5e-4 + 0.6*98*5e-4


def test_an_unknown_exit_reason_never_closes_a_signal() -> None:
    sig, entry = signal(), intent()
    apply_entry(sig, entry, evidence(entry), D("0.6"), when=NOW)
    leg = intent(reduce_only=True, leg_key="tp1:0", side="SELL")
    with pytest.raises(TrendStateBlocked, match="exit reason"):
        apply_exit(sig, leg, evidence(leg), D("0"), when=NOW)
