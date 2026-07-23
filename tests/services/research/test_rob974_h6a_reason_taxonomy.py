"""ROB-974 R3 boundary fix -- the attempt reason taxonomy must stay closed
AND consistent across its two deliberate copies.

``app/services/rob974_h6a_bridge.REASON_ALLOWLIST_BY_STATUS`` literally
duplicates ``rob974_h6a_evidence.ALLOWED_REASONS_BY_STATUS`` on purpose (the
app boundary never imports the pure research module).  A deliberate
duplication still needs a guard: without one, adding a reason on one side
leaves the other silently rejecting it, which is exactly the class of
half-applied fix this change set exists to close.
"""

from __future__ import annotations

from app.services import rob974_h6a_bridge as bridge
from app.services import rob974_h6b_materializer as materializer

_evidence = materializer.h6a_evidence


def test_bridge_allowlist_is_byte_identical_to_research_taxonomy() -> None:
    assert bridge.REASON_ALLOWLIST_BY_STATUS == _evidence.ALLOWED_REASONS_BY_STATUS


def test_horizon_rejection_is_a_bookable_rejected_reason() -> None:
    """The engine relabel is inert unless the reason is also bookable.

    Before this fix a phase-boundary rejection could only be booked as one of
    the two data-gap reasons; a walk truncated by its fold horizon would have
    been unrepresentable once the engine stopped calling it a data gap.
    """
    assert _evidence.REASON_FOLD_HORIZON_REJECTED == "rejected:fold_horizon_rejected"
    assert (
        _evidence.REASON_FOLD_HORIZON_REJECTED
        in _evidence.ALLOWED_REASONS_BY_STATUS["rejected"]
    )
    assert (
        _evidence.REASON_FOLD_HORIZON_REJECTED
        in bridge.REASON_ALLOWLIST_BY_STATUS["rejected"]
    )


def test_horizon_rejection_stays_distinct_from_the_data_gap_reasons() -> None:
    """A phase truncation is not a corpus defect -- never collapse them."""
    assert _evidence.REASON_FOLD_HORIZON_REJECTED not in (
        _evidence.REASON_DATA_GAP_IN_POSITION,
        _evidence.REASON_DATA_GAP_IN_PAIR_POSITION,
    )


def test_horizon_rejection_is_not_bookable_under_any_other_status() -> None:
    for status, allowed in _evidence.ALLOWED_REASONS_BY_STATUS.items():
        if status == "rejected":
            continue
        assert _evidence.REASON_FOLD_HORIZON_REJECTED not in allowed
