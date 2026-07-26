from __future__ import annotations

import typing

import pytest
import reason_codes as rc


def test_all_reason_codes_matches_the_reason_code_literal_exactly():
    """Drift guard: ``ALL_REASON_CODES`` (the runtime-checkable frozenset)
    must be in exact 1:1 sync with the ``ReasonCode`` Literal's arguments —
    if someone adds a new reason to the Literal (for documentation/typing)
    without adding it to the frozenset, ``reconcile_histogram`` would reject
    a legitimate new code, or vice versa a frozenset-only addition would
    never be caught by a type checker."""
    literal_args = set(typing.get_args(rc.ReasonCode))
    assert literal_args == rc.ALL_REASON_CODES


def test_every_reason_code_has_exactly_one_mapped_action():
    assert set(rc.ACTION_FOR_REASON) == rc.ALL_REASON_CODES
    for action in rc.ACTION_FOR_REASON.values():
        assert action in ("ENTER", "EXIT", "HOLD", "NO_ACTION")


def test_reconcile_histogram_sums_to_input_length():
    codes = ["ENTRY_ACCEPTED", "ENTRY_ACCEPTED", "NO_ENTRY_SIGNAL", "HYSTERESIS_HOLD"]
    histogram = rc.reconcile_histogram(codes)
    assert histogram == {
        "ENTRY_ACCEPTED": 2,
        "NO_ENTRY_SIGNAL": 1,
        "HYSTERESIS_HOLD": 1,
    }
    assert sum(histogram.values()) == len(codes)


def test_reconcile_histogram_empty_input_is_the_empty_histogram():
    assert rc.reconcile_histogram([]) == {}


def test_reconcile_histogram_rejects_unknown_code_fail_closed():
    with pytest.raises(rc.UnknownReasonCodeError, match="TYPO_CODE"):
        rc.reconcile_histogram(["ENTRY_ACCEPTED", "TYPO_CODE"])
