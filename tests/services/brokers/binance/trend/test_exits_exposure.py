"""Exit priority/bounds and account-wide exposure rules; pure, no I/O."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.services.brokers.binance.futures_demo.dto import FuturesDemoOpenOrdersResult
from app.services.brokers.binance.trend.constants import (
    EXIT_HARD_STOP,
    EXIT_REASONS,
    EXIT_SIGNAL_FLIP,
    EXIT_TIME_EXIT,
    UnknownExitReason,
    assert_exit_reason,
)
from app.services.brokers.binance.trend.exits import (
    HardStopObservation,
    HeldPosition,
    choose_exit,
    hard_stop_touched,
)
from app.services.brokers.binance.trend.exposure import (
    ConflictReason,
    TrendExposureMismatch,
    TrendExposureUnavailable,
    entry_conflicts,
    exit_quantity,
)
from tests.services.brokers.binance.trend.fakes import open_order, position

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 14, 4, tzinfo=dt.UTC)
ENTERED = NOW - dt.timedelta(hours=8)
NOBODY = FuturesDemoOpenOrdersResult(orders=[])


def held(side="BUY", qty="1.0", step="0.001", entered=ENTERED) -> HeldPosition:
    return HeldPosition(
        side=side,
        entry_price=D("100"),
        entered_at=entered,
        broker_remaining_qty=D(qty),
        step_size=D(step),
    )


def calm(**changes) -> HardStopObservation:
    values = {
        "minutes_ok": True,
        "minute_low": D("99"),
        "minute_high": D("101"),
        "bar_close": D("100"),
        "quote": D("100"),
    }
    values.update(changes)
    return HardStopObservation(**values)


def decide(h=None, *, observed=None, signal=None, now=NOW, stop="95"):
    return choose_exit(
        h or held(),
        stop_price=D(stop),
        observed=observed or calm(),
        signal_side=signal,
        now=now,
    )


def test_the_exit_vocabulary_is_closed() -> None:
    assert EXIT_REASONS == (EXIT_HARD_STOP, EXIT_SIGNAL_FLIP, EXIT_TIME_EXIT)
    for reason in EXIT_REASONS:
        assert assert_exit_reason(reason) == reason
    for bad in ("tp1", "bar_close_stop", "", None, 3, "HARD_STOP"):
        with pytest.raises(UnknownExitReason):
            assert_exit_reason(bad)


def test_nothing_to_do_when_calm_and_the_signal_agrees() -> None:
    assert decide(signal="BUY") is None
    assert decide(signal=None) is None


def test_priority_is_hard_stop_then_signal_flip_then_time_exit() -> None:
    old = NOW - dt.timedelta(days=31)
    everything = decide(
        held(entered=old), observed=calm(minute_low=D("94")), signal="SELL"
    )
    assert everything.reason == EXIT_HARD_STOP
    flip_and_time = decide(held(entered=old), signal="SELL")
    assert flip_and_time.reason == EXIT_SIGNAL_FLIP
    assert decide(held(entered=old), signal="BUY").reason == EXIT_TIME_EXIT


def test_time_exit_fires_at_thirty_days_and_not_before() -> None:
    just_under = NOW - dt.timedelta(days=30) + dt.timedelta(seconds=1)
    assert decide(held(entered=just_under)) is None
    assert decide(held(entered=NOW - dt.timedelta(days=30))).reason == EXIT_TIME_EXIT


def test_a_flip_needs_a_known_opposite_signal() -> None:
    assert decide(signal="SELL").reason == EXIT_SIGNAL_FLIP
    short = held("SELL")
    assert decide(short, signal="BUY", stop="105").reason == EXIT_SIGNAL_FLIP
    assert decide(short, signal="SELL", stop="105") is None
    assert decide(signal=None) is None  # NO_SIGNAL never closes a position


@pytest.mark.parametrize(
    ("side", "observed", "touched"),
    [
        ("BUY", {"minute_low": D("95")}, True),  # inclusive boundary
        ("BUY", {"minute_low": D("95.01")}, False),
        ("BUY", {"quote": D("94.9")}, True),
        ("BUY", {"bar_close": D("94")}, True),
        ("SELL", {"minute_high": D("105")}, True),
        ("SELL", {"minute_high": D("104.99")}, False),
        ("SELL", {"quote": D("105.5")}, True),
        ("SELL", {"bar_close": D("106")}, True),
    ],
)
def test_hard_stop_is_judged_on_the_losing_side_with_a_inclusive_boundary(
    side, observed, touched
) -> None:
    stop = D("95") if side == "BUY" else D("105")
    assert (
        hard_stop_touched(side=side, stop_price=stop, observed=calm(**observed))
        is touched
    )


def test_a_failed_1m_fetch_falls_back_to_the_4h_decision_only() -> None:
    blind = HardStopObservation(
        minutes_ok=False,
        minute_low=None,
        minute_high=None,
        bar_close=D("100"),
        quote=D("90"),  # even a stopped-out quote is ignored in fallback mode
    )
    assert decide(observed=blind) is None
    stopped_4h = HardStopObservation(False, None, None, D("94"), D("100"))
    assert decide(observed=stopped_4h).reason == EXIT_HARD_STOP


def test_exit_quantity_is_the_broker_proven_remainder_floored_to_the_step() -> None:
    assert decide(held(qty="1.2349"), observed=calm(quote=D("90"))).qty == D("1.234")
    assert decide(held(qty="0.0005"), observed=calm(quote=D("90"))) is None  # dust


def test_choose_exit_rejects_naive_clocks_and_bad_prices() -> None:
    with pytest.raises(ValueError):
        decide(now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        choose_exit(
            held(), stop_price=D("0"), observed=calm(), signal_side=None, now=NOW
        )
    assert decide(held(qty="0")) is None


# ------------------------------------------------------------- exposure


def positions(**amounts):
    return [position(symbol, amt) for symbol, amt in amounts.items()]


def test_a_flat_account_is_clear_and_foreign_exposure_on_the_symbol_blocks() -> None:
    flat = positions(BTCUSDT=0, ETHUSDT=0)
    assert entry_conflicts(symbol="BTCUSDT", positions=flat, open_orders=NOBODY) == ()
    foreign = positions(BTCUSDT="-0.5")
    assert entry_conflicts(symbol="BTCUSDT", positions=foreign, open_orders=NOBODY) == (
        ConflictReason.FOREIGN_POSITION,
    )
    orders = FuturesDemoOpenOrdersResult(orders=[open_order("BTCUSDT")])
    assert entry_conflicts(symbol="BTCUSDT", positions=flat, open_orders=orders) == (
        ConflictReason.FOREIGN_OPEN_ORDER,
    )


def test_exposure_on_another_symbol_is_not_a_conflict_for_this_one() -> None:
    mixed = positions(BTCUSDT=0, ETHUSDT="3")
    orders = FuturesDemoOpenOrdersResult(orders=[open_order("ETHUSDT")])
    assert entry_conflicts(symbol="BTCUSDT", positions=mixed, open_orders=orders) == ()


def test_a_non_1x_foreign_position_is_reported_with_the_conflict() -> None:
    rows = [position("BTCUSDT", "2", leverage=5)]
    assert entry_conflicts(symbol="BTCUSDT", positions=rows, open_orders=NOBODY) == (
        ConflictReason.FOREIGN_POSITION,
        ConflictReason.POSITION_NOT_ONE_WAY_1X,
    )


@pytest.mark.parametrize(
    ("rows", "orders"),
    [
        (None, NOBODY),
        ([], None),
        ("rows", NOBODY),
        ([position("BTCUSDT", 0), position("BTCUSDT", 1)], NOBODY),
    ],
)
def test_incomplete_or_duplicate_account_data_is_never_read_as_flat(
    rows, orders
) -> None:
    with pytest.raises(TrendExposureUnavailable):
        entry_conflicts(symbol="BTCUSDT", positions=rows, open_orders=orders)
    with pytest.raises(TrendExposureUnavailable):
        exit_quantity(
            symbol="BTCUSDT",
            side="BUY",
            expected_remaining=D("1"),
            positions=rows,
            open_orders=orders,
        )


def test_exit_quantity_is_the_lane_remainder_and_only_when_the_broker_agrees() -> None:
    args = {"symbol": "BTCUSDT", "open_orders": NOBODY}
    assert exit_quantity(
        side="BUY",
        expected_remaining=D("0.8"),
        positions=positions(BTCUSDT="0.8"),
        **args,
    ) == D("0.8")
    assert exit_quantity(
        side="SELL",
        expected_remaining=D("0.8"),
        positions=positions(BTCUSDT="-0.8"),
        **args,
    ) == D("0.8")
    # The state can only reconcile the post-fill position against the lane
    # remainder, so a broker position that differs either way is refused.
    for broker in ("1.0", "0.5"):
        with pytest.raises(TrendExposureMismatch, match="differs"):
            exit_quantity(
                side="BUY",
                expected_remaining=D("0.8"),
                positions=positions(BTCUSDT=broker),
                **args,
            )


@pytest.mark.parametrize(
    ("side", "amount"), [("BUY", "0"), ("BUY", "-1"), ("SELL", "0"), ("SELL", "1")]
)
def test_an_exit_is_refused_when_the_broker_does_not_show_the_held_side(
    side, amount
) -> None:
    with pytest.raises(TrendExposureMismatch):
        exit_quantity(
            symbol="BTCUSDT",
            side=side,
            expected_remaining=D("1"),
            positions=positions(BTCUSDT=amount),
            open_orders=NOBODY,
        )


def test_an_exit_is_refused_with_an_open_order_or_a_non_1x_position() -> None:
    with pytest.raises(TrendExposureMismatch):
        exit_quantity(
            symbol="BTCUSDT",
            side="BUY",
            expected_remaining=D("1"),
            positions=positions(BTCUSDT="1"),
            open_orders=FuturesDemoOpenOrdersResult(orders=[open_order("BTCUSDT")]),
        )
    with pytest.raises(TrendExposureMismatch):
        exit_quantity(
            symbol="BTCUSDT",
            side="BUY",
            expected_remaining=D("1"),
            positions=[position("BTCUSDT", "1", leverage=3)],
            open_orders=NOBODY,
        )


def test_the_lanes_own_working_order_does_not_block_an_exit_but_a_foreign_one_does() -> (
    None
):
    own = FuturesDemoOpenOrdersResult(orders=[open_order("BTCUSDT", cid="trend-own")])
    args = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "expected_remaining": D("0.3"),
        "positions": positions(BTCUSDT="0.3"),
    }
    assert exit_quantity(
        open_orders=own, own_order_ids=frozenset({"trend-own"}), **args
    ) == D("0.3")
    with pytest.raises(TrendExposureMismatch, match="foreign"):
        exit_quantity(open_orders=own, **args)
    mixed = FuturesDemoOpenOrdersResult(
        orders=[
            open_order("BTCUSDT", cid="trend-own"),
            open_order("BTCUSDT", cid="x-1"),
        ]
    )
    with pytest.raises(TrendExposureMismatch, match="foreign"):
        exit_quantity(open_orders=mixed, own_order_ids=frozenset({"trend-own"}), **args)
