"""Acceptance 2: an availability violation raises (fail closed)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pytest

from research.kr_delist_repair.causal import (
    CausalTable,
    assert_available,
    bar_available_at,
    causal_join,
)
from research.kr_delist_repair.model import AvailabilityViolation

D = date(2020, 3, 2)
BAR_AT = bar_available_at(D)
TERMINAL_AT = datetime(2020, 3, 3, tzinfo=UTC)


def _table() -> pa.Table:
    return pa.table(
        {
            "symbol": ["000001", "000002"],
            "session": pa.array([D, D], pa.date32()),
            "close": [100, 200],
            "available_at": pa.array([BAR_AT, BAR_AT], pa.timestamp("us", tz="UTC")),
            "terminal_outcome": [None, "statutory_cause"],
            "terminal_available_at": pa.array(
                [None, TERMINAL_AT], pa.timestamp("us", tz="UTC")
            ),
        }
    )


def _causal() -> CausalTable:
    return CausalTable.for_panel(_table())


def test_bar_is_available_at_16_kst() -> None:
    assert BAR_AT.astimezone(UTC) == datetime(2020, 3, 2, 7, 0, tzinfo=UTC)


def test_reading_a_bar_before_its_available_at_raises() -> None:
    before = BAR_AT - timedelta(minutes=1)
    with pytest.raises(AvailabilityViolation, match="after decision_time"):
        _causal().lookup(["000001", D], before)


def test_reading_a_bar_at_or_after_available_at_succeeds() -> None:
    row = _causal().lookup(["000001", D], BAR_AT, ["close"])
    assert row == {"close": 100}


def test_terminal_column_has_its_own_gate() -> None:
    table = _causal()
    # The bar itself is available at the close ...
    assert table.lookup(["000002", D], BAR_AT, ["close"]) == {"close": 200}
    # ... but its terminal flag is not known until the delisting is public.
    with pytest.raises(AvailabilityViolation, match="terminal_outcome"):
        table.lookup(["000002", D], BAR_AT, ["terminal_outcome"])
    later = table.lookup(["000002", D], TERMINAL_AT, ["terminal_outcome"])
    assert later == {"terminal_outcome": "statutory_cause"}


def test_join_fails_closed_instead_of_dropping_the_row() -> None:
    left = [
        {"symbol": "000001", "session": D, "decision_time": BAR_AT},
        {"symbol": "000002", "session": D, "decision_time": BAR_AT},
    ]
    with pytest.raises(AvailabilityViolation):
        causal_join(
            left,
            _causal(),
            left_key=["symbol", "session"],
            decision_col="decision_time",
            columns=["close", "terminal_outcome"],
        )


def test_join_succeeds_when_every_row_is_available() -> None:
    left = [{"symbol": "000002", "session": D, "decision_time": TERMINAL_AT}]
    out = causal_join(
        left,
        _causal(),
        left_key=["symbol", "session"],
        decision_col="decision_time",
        columns=["close", "terminal_outcome"],
    )
    assert out[0]["terminal_outcome"] == "statutory_cause"


def test_naive_decision_time_raises() -> None:
    with pytest.raises(AvailabilityViolation, match="timezone-aware"):
        assert_available(BAR_AT, datetime(2020, 3, 3), "x")


def test_unknown_availability_raises() -> None:
    with pytest.raises(AvailabilityViolation, match="unknown"):
        assert_available(None, TERMINAL_AT, "x")


def test_join_rejects_a_non_datetime_decision() -> None:
    with pytest.raises(AvailabilityViolation):
        causal_join(
            [{"symbol": "000001", "session": D, "decision_time": "2020-03-03"}],
            _causal(),
            left_key=["symbol", "session"],
            decision_col="decision_time",
            columns=["close"],
        )
