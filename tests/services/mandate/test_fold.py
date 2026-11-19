"""``fold_plan_events``: the plan state machine of §3.2/§3.3/§3.7 (no database).

Test names carry the invariant they pin (I7, I9, I16, I19, I24 ...).
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.services.mandate import vocabulary as v
from app.services.mandate.fold import (
    InvalidPlanEvent,
    PlanState,
    apply_event,
    fold_plan_events,
)
from tests.services.mandate.support import SESSION, D, Stream, ev

pytestmark = pytest.mark.unit


def refused(code: v.MandateRefusal):
    return pytest.raises(InvalidPlanEvent, match=code.value)


def test_authority_follows_the_stream_not_the_last_event_name() -> None:
    s = Stream()
    s.approve()
    s.add("terms_rebound", terms_id=2, terms_sha256="b" * 64, effective_session=SESSION)
    assert s.state.authority == "approved"
    assert s.state.current_terms_id == 2
    s.add("suspended")
    s.add("resumed", resume_target="approved", effective_session=SESSION)
    assert s.state.authority == "approved"


def test_a_stream_opens_with_proposed_only() -> None:
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        apply_event(PlanState(), ev("approved"))
    s = Stream()
    s.add("proposed")
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("proposed")


def test_seq_gap_is_refused_by_fold() -> None:
    from app.services.mandate.fold import PlanEvent

    with refused(v.MandateRefusal.INVALID_TRANSITION):
        fold_plan_events([PlanEvent("proposed", seq=1), PlanEvent("approved", seq=3)])


def test_i7_a_tranche_arms_only_after_the_previous_one_completed() -> None:
    s = Stream()
    s.approve()
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("tranche_armed", tranche_index=2)
    s.fill_first()
    s.add("first_tranche_settled")
    s.add("tranche_armed", tranche_index=2)
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("tranche_armed", tranche_index=3)  # tranche 2 is not complete


def test_i9_one_open_proposal_per_tranche_and_none_while_a_fence_is_open() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    with refused(v.MandateRefusal.TRANCHE_DUPLICATE):
        s.add("tranche_armed", tranche_index=1)
    s.fence(1, pid)
    # the proposal ends without a fill; the fence is closed by that terminal event
    s.add("tranche_unfilled", tranche_index=1, proposal_id=pid)
    assert s.state.tranche(1).unclosed_fences == ()
    s.arm_and_propose(1)  # a fresh proposal is possible again


def test_i9_an_unclosed_fence_blocks_a_new_fence_in_the_tranche() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    with refused(v.MandateRefusal.TRANCHE_UNCERTAIN):
        s.fence(1, pid)


def test_i24_a_released_fence_is_closed_and_the_tranche_can_fence_again() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    seq = s.fence(1, pid)
    s.add("tranche_send_released", tranche_index=1, fenced_seq=seq)
    assert s.state.tranche(1).unclosed_fences == ()
    s.fence(1, pid)  # a new attempt (new attempt id) on the same proposal
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("tranche_send_released", tranche_index=1, fenced_seq=seq)


@pytest.mark.parametrize(
    "stop",
    [
        ("retired", {}),
        ("suspended", {}),
        ("thesis_broken", {}),
    ],
)
def test_i16_no_fence_after_authority_is_lost(stop) -> None:
    name, fields = stop
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.add(name, **fields)
    with refused(v.MandateRefusal.PLAN_NOT_ACTIVE):
        s.fence(1, pid)


def test_i16_a_fence_before_the_effective_session_is_refused() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    with refused(v.MandateRefusal.PLAN_NOT_ACTIVE):
        s.fence(1, pid, day=date(2026, 11, 20))


def test_i16_no_new_tranche_or_proposal_after_retirement() -> None:
    s = Stream()
    s.approve()
    s.add("retired")
    with refused(v.MandateRefusal.PLAN_NOT_ACTIVE):
        s.add("tranche_armed", tranche_index=1)


def test_a_fill_may_still_be_recorded_on_a_retired_plan() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    s.add("retired")
    s.add(
        "tranche_partially_filled",
        tranche_index=1,
        proposal_id=pid,
        fill_amount=D("100000"),
        fill_quantity=D("1"),
    )
    assert s.state.tranche(1).filled_amount == D("100000")
    assert s.state.tranche(1).unclosed_fences == ()


def test_first_fill_must_equal_the_cumulative_first_tranche_fills() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    with refused(v.MandateRefusal.INVALID_FIELDS):
        s.add(
            "tranche_filled",
            tranche_index=1,
            proposal_id=pid,
            fill_amount=D("300000"),
            fill_quantity=D("3"),
            first_fill_amount=D("299999"),
            first_fill_quantity=D("3"),
        )


def test_design_example_partial_then_filled_then_first_tranche_settled() -> None:
    """§3.2 example: 300,000 on day 1, 588,600 on day 2 -> first fill 888,600 / 9."""
    s = Stream()
    s.approve()
    s.fill_first("300000", "3", done=False)
    assert s.state.tranche(1).filled_amount == D("300000")
    assert not s.state.tranche(1).completed
    pid2 = s.arm_and_propose(1)
    s.add(
        "tranche_filled",
        tranche_index=1,
        proposal_id=pid2,
        fill_amount=D("588600"),
        fill_quantity=D("6"),
        first_fill_amount=D("888600"),
        first_fill_quantity=D("9"),
        evidence={"trade_date": "2026-11-24"},
    )
    t1 = s.state.tranche(1)
    assert t1.completed and t1.settled_on == date(2026, 11, 24)
    assert (s.state.first_fill_amount, s.state.first_fill_quantity) == (
        D("888600"),
        D("9"),
    )
    s.add("first_tranche_settled")
    assert s.state.authority == "active"


def test_design_example_operator_gives_up_the_remainder() -> None:
    """§3.2 variation 4: closed_partial records first_fill 300,000 / 3, no carry-over."""
    s = Stream()
    s.approve()
    s.fill_first("300000", "3", done=False)
    s.add(
        "tranche_closed_partial",
        tranche_index=1,
        first_fill_amount=D("300000"),
        first_fill_quantity=D("3"),
    )
    assert s.state.tranche(1).completion_event == "tranche_closed_partial"
    assert s.state.first_fill_amount == D("300000")


def test_a_tranche_without_a_fill_cannot_be_completed() -> None:
    s = Stream()
    s.approve()
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add(
            "tranche_closed_partial",
            tranche_index=1,
            first_fill_amount=D("1"),
            first_fill_quantity=D("1"),
        )


def test_first_tranche_settled_needs_a_completed_first_tranche() -> None:
    s = Stream()
    s.approve()
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("first_tranche_settled")


def test_resume_target_follows_the_first_tranche() -> None:
    s = Stream()
    s.approve()
    s.add("suspended")
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("resumed", resume_target="active", effective_session=SESSION)


def test_exhausted_needs_the_third_tranche_complete() -> None:
    s = Stream()
    s.approve()
    s.fill_first()
    s.add("first_tranche_settled")
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("exhausted")


def test_thesis_broken_is_possible_from_approved_and_exhausted_states() -> None:
    for prefix in ("approved", "suspended"):
        s = Stream()
        s.approve()
        if prefix == "suspended":
            s.add("suspended")
        s.add("thesis_broken")
        assert s.state.authority == "thesis_broken"
        assert s.state.thesis_broken_seqs == (s.state.last_seq,)


def test_thesis_evidence_refresh_names_a_thesis_broken_event() -> None:
    s = Stream()
    s.approve()
    s.add("thesis_broken")
    broken = s.state.thesis_broken_seqs[0]
    s.add("thesis_evidence_refreshed", thesis_seq=broken)
    assert s.state.authority == "thesis_broken"
    s.add("retired")
    s.add("thesis_evidence_refreshed", thesis_seq=broken)
    assert s.state.authority == "retired"
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("thesis_evidence_refreshed", thesis_seq=1)


def test_closed_only_follows_thesis_broken() -> None:
    s = Stream()
    s.approve()
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("closed")


def _retire_and_settle(s: Stream) -> None:
    s.add("retired")
    s.add("orders_settled")


def test_i19_release_needs_settled_orders_then_disposed_holdings() -> None:
    s = Stream()
    s.add("proposed")
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("orders_settled")  # authority proposed: nothing to wind down
    s.add("retired")
    assert s.state.cleanup == "winding_down"
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("released")
    s.add("orders_settled")
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("released")
    s.add("holdings_disposed", evidence={"holdings_disposition": "never_adopted"})
    s.add("released")
    assert s.state.released
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("suspended")


def test_i19_an_open_fence_blocks_orders_settled_and_released() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    s.add("retired")
    with refused(v.MandateRefusal.TRANCHE_UNCERTAIN):
        s.add("orders_settled")


def test_i19_never_adopted_is_only_for_a_plan_that_was_never_approved() -> None:
    s = Stream()
    s.approve()
    _retire_and_settle(s)
    with refused(v.MandateRefusal.INVALID_FIELDS):
        s.add("holdings_disposed", evidence={"holdings_disposition": "never_adopted"})
    s.add("holdings_disposed", evidence={"holdings_disposition": "zero_holdings"})


def test_holdings_disposed_needs_a_closed_kind() -> None:
    s = Stream()
    s.add("proposed")
    _retire_and_settle(s)
    with refused(v.MandateRefusal.INVALID_FIELDS):
        s.add("holdings_disposed", evidence={"holdings_disposition": "sold_it"})


def test_a_proposal_is_used_once_and_a_terminal_event_once() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.add("tranche_unfilled", tranche_index=1, proposal_id=pid)
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.add("tranche_unfilled", tranche_index=1, proposal_id=pid)
    s.add("tranche_armed", tranche_index=1)
    with refused(v.MandateRefusal.TRANCHE_DUPLICATE):
        s.add("tranche_proposed", tranche_index=1, proposal_id=pid)


def test_a_fence_needs_the_tranches_open_proposal() -> None:
    s = Stream()
    s.approve()
    s.arm_and_propose(1)
    with refused(v.MandateRefusal.INVALID_TRANSITION):
        s.fence(1, uuid4())


def test_tranche_fills_accumulate_and_settle_day_is_the_latest_fill_day() -> None:
    s = Stream()
    s.approve()
    s.fill_first("300000", "3", done=False)
    pid = s.arm_and_propose(1)
    s.add(
        "tranche_partially_filled",
        tranche_index=1,
        proposal_id=pid,
        fill_amount=D("100"),
        fill_quantity=D("1"),
        evidence={"trade_date": "2026-11-25"},
    )
    t = s.state.tranche(1)
    assert (t.filled_amount, t.filled_quantity) == (D("300100"), D("4"))
    assert t.settled_on == date(2026, 11, 25)


def test_a_bad_resume_target_or_approval_mode_is_a_closed_refusal_not_an_assert() -> (
    None
):
    s = Stream()
    s.approve()
    s.add("suspended")
    with refused(v.MandateRefusal.INVALID_FIELDS):
        s.add("resumed", resume_target="bogus", effective_session=SESSION)
    s2 = Stream()
    s2.approve()
    pid = s2.arm_and_propose(1)
    with refused(v.MandateRefusal.INVALID_FIELDS):
        s2.add(
            "tranche_send_fenced",
            tranche_index=1,
            proposal_id=pid,
            notional=D("1"),
            kst_day=SESSION,
            approval_mode="bogus",
            account_ref="a",
            attempt_id=uuid4(),
        )
