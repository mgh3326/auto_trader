from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from inspect import signature
from pathlib import Path

import pytest

from research.intraday_harness_v2 import (
    CONTRACT_HASH,
    CONTRACT_VERSION,
    Bar,
    BarSeries,
    IncompleteReason,
    Side,
    Signal,
    run,
)

T0 = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
INTERVAL = timedelta(minutes=5)


def bar(offset: int, *, complete: bool = True, open_price: str = "101") -> Bar:
    start = T0 + offset * INTERVAL
    return Bar(
        "ABC",
        start,
        start + INTERVAL,
        Decimal(open_price),
        Decimal("103"),
        Decimal("99"),
        Decimal("102"),
        complete,
    )


def signal() -> Signal:
    return Signal("ABC", T0 + INTERVAL, Side.BUY, Decimal("2"), INTERVAL)


def test_contract_is_frozen_and_covers_enforcement_code():
    assert CONTRACT_VERSION == "intraday-harness-v2.0.0"
    assert len(CONTRACT_HASH) == 64
    engine = Path(__file__).parents[1] / "engine.py"
    assert sha256(engine.read_bytes()).hexdigest() != "0" * 64


def test_fill_bar_and_fill_price_are_not_inputs_to_execution_api():
    parameters = signature(run).parameters
    assert "fill_bar" not in parameters
    assert "fill_price" not in parameters


def test_fill_is_derived_from_strictly_next_bar_open_and_costs_are_separate():
    result = run(
        [signal()],
        BarSeries.from_iterable([bar(0, open_price="100"), bar(1, open_price="101")]),
        fee_bps=5,
        slippage_bps=10,
    )
    fill = result.fills[0]
    assert result.status == "COMPLETE"
    assert fill.fill_bar_open_time == T0 + INTERVAL
    assert fill.fill_price == Decimal("101")
    assert fill.fee == Decimal("0.101")
    assert fill.slippage == Decimal("0.202")
    assert result.fee_total == Decimal("0.101")
    assert result.slippage_total == Decimal("0.202")
    assert result.signal_count == 1
    assert result.filled_count == 1
    assert result.filled_notional == Decimal("202")


def test_signal_bar_close_mismatch_blocks_heterogeneous_interval_lookahead():
    signal_bar = Bar(
        "ABC",
        datetime(2026, 1, 2, 9, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        Decimal("100"),
        Decimal("200"),
        Decimal("99"),
        Decimal("150"),
    )
    overlapping_fill_bar = Bar(
        "ABC",
        datetime(2026, 1, 2, 9, 5, tzinfo=UTC),
        datetime(2026, 1, 2, 9, 10, tzinfo=UTC),
        Decimal("101"),
        Decimal("103"),
        Decimal("99"),
        Decimal("102"),
    )
    result = run(
        [
            Signal(
                "ABC",
                datetime(2026, 1, 2, 9, 5, tzinfo=UTC),
                Side.BUY,
                Decimal("1"),
                timedelta(minutes=5),
            )
        ],
        BarSeries.from_iterable([signal_bar, overlapping_fill_bar]),
        fee_bps=0,
        slippage_bps=0,
    )
    assert result.status == "INCOMPLETE"
    assert result.fills[0].fill_price is None
    assert result.fills[0].quantity == Decimal(0)
    assert (
        result.fills[0].incomplete_reason == IncompleteReason.SIGNAL_BAR_CLOSE_MISMATCH
    )


def test_session_gap_has_distinct_reason_and_empty_stream_is_not_complete():
    overnight = bar(0)
    next_session = Bar(
        "ABC",
        datetime(2026, 1, 3, 10, 0, tzinfo=UTC),
        datetime(2026, 1, 3, 10, 5, tzinfo=UTC),
        Decimal("101"),
        Decimal("103"),
        Decimal("99"),
        Decimal("102"),
    )
    gap_result = run(
        [signal()],
        BarSeries.from_iterable([overnight, next_session]),
        fee_bps=0,
        slippage_bps=0,
    )
    assert (
        gap_result.fills[0].incomplete_reason == IncompleteReason.NEXT_BAR_SESSION_GAP
    )
    assert gap_result.filled_count == 0
    assert gap_result.filled_notional == Decimal(0)

    empty_result = run(iter(()), BarSeries.from_iterable([]), fee_bps=0, slippage_bps=0)
    assert empty_result.status == "NO_SIGNALS"
    assert empty_result.signal_count == 0


def test_same_bar_fill_cannot_be_requested_and_missing_next_bar_is_incomplete():
    # Supplying only the signal bar cannot cause a same-bar fill: run derives
    # the required open at bar_close_time and reports it missing.
    result = run(
        [signal()],
        BarSeries.from_iterable([bar(0, open_price="100")]),
        fee_bps=0,
        slippage_bps=1,
    )
    assert result.status == "INCOMPLETE"
    assert result.fills[0].fill_price is None
    assert result.fills[0].incomplete_reason == IncompleteReason.NEXT_BAR_MISSING


def test_forward_fill_is_not_an_available_path_and_incomplete_reaches_summary():
    # A gap at offset 1 is not synthesized from offset 0 or offset 2.
    result = run(
        [signal()],
        BarSeries.from_iterable([bar(0), bar(2)]),
        fee_bps=1,
        slippage_bps=1,
    )
    assert result.status == "INCOMPLETE"
    assert result.incomplete_count == 1
    assert result.filled_count == 0
    assert result.fills[0].quantity == Decimal(0)
    assert result.fee_total == Decimal(0)
    assert result.slippage_total == Decimal(0)


def test_incomplete_bar_is_not_used():
    result = run(
        [signal()],
        BarSeries.from_iterable([bar(0), bar(1, complete=False)]),
        fee_bps=1,
        slippage_bps=1,
    )
    assert result.fills[0].incomplete_reason == IncompleteReason.NEXT_BAR_INCOMPLETE
    assert result.status == "INCOMPLETE"


def test_zero_slippage_is_explicit_and_deterministic():
    bars = BarSeries.from_iterable([bar(0), bar(1)])
    first = run([signal()], bars, fee_bps=0, slippage_bps=0)
    second = run([signal()], bars, fee_bps=0, slippage_bps=0)
    assert first == second
    assert first.slippage_total == Decimal(0)


def test_negative_cost_model_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        run(
            [signal()],
            BarSeries.from_iterable([bar(0), bar(1)]),
            fee_bps=-1,
            slippage_bps=0,
        )
