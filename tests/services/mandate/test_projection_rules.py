"""``projection_rules.project``: ledger fills -> plan events (hk 3795 T4b, no database).

Test names carry the invariant they pin (I10 fills are ledger evidence only, I19
release needs settled orders and a scope-wide zero holdings read). Each failure
direction asserts that **no** event is returned, not merely a hold code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.services.mandate.fold import PlanState
from app.services.mandate.projection_rules import (
    FillFact,
    HoldingsReading,
    PlanFacts,
    ProjectionHold,
    ProposalFact,
    RungFact,
    project,
    rung_status,
    tranche_event_for,
)
from tests.services.mandate.support import SESSION, D, Stream

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
PLAN_ACCT = "acct-plan"
OTHER_ACCT = "acct-other"
SYMBOL = "005930"
AMOUNTS = (D("940000"), D("705000"), D("705000"))


def rung(
    rung_id: int,
    state: str = "expired",
    *,
    quantity: str = "10",
    limit: str | None = "100000",
    filled: str | None = None,
    order_id: str | None = "ORD",
    key: str | None = "idem",
) -> RungFact:
    return RungFact(
        rung_id=rung_id,
        state=state,
        quantity=D(quantity),
        limit_price=None if limit is None else D(limit),
        filled_qty=None if filled is None else D(filled),
        broker_order_id=order_id,
        idempotency_key=key,
    )


def fill(
    ledger_id: int,
    rung_id: int,
    qty: str,
    price: str,
    day: date = date(2026, 11, 23),
) -> FillFact:
    return FillFact(
        ledger_id=ledger_id,
        rung_id=rung_id,
        quantity=D(qty),
        price=D(price),
        trade_date=day,
        filled_at=datetime(day.year, day.month, day.day, 1, 0, tzinfo=UTC),
    )


def proposal(pid: UUID, rungs: list[RungFact], fills: list[FillFact] | None = None):
    return ProposalFact(proposal_id=pid, rungs=tuple(rungs), fills=tuple(fills or ()))


def reading(
    account: str, qty: str = "0", *, at: datetime | None = None, symbol: str = SYMBOL
) -> HoldingsReading:
    return HoldingsReading(
        account_ref=account,
        symbol=symbol,
        quantity=D(qty),
        checked_at=at or NOW - timedelta(minutes=10),
    )


def facts(
    proposals: list[ProposalFact] | None = None,
    holdings: list[HoldingsReading] | None = None,
    **over,
) -> PlanFacts:
    base = {
        "plan_account_ref": PLAN_ACCT,
        "scope_account_refs": (PLAN_ACCT, OTHER_ACCT),
        "symbol": SYMBOL,
        "tranche_amounts": AMOUNTS,
        "proposals": tuple(proposals or ()),
        "holdings": tuple(holdings or ()),
        "now": NOW,
    }
    base.update(over)
    return PlanFacts(**base)


def names(plan) -> list[str]:
    return [e.event for e in plan.events]


def codes(plan) -> list[str]:
    return [h.code.value for h in plan.holds]


def apply(stream: Stream, plan) -> None:
    for planned in plan.events:
        stream.add(planned.event, **planned.fields, evidence=planned.evidence)


# ----------------------------------------------------------------- §3.2 fixed example
def test_fixed_example_partial_fill_then_remainder_below_one_share_completes_the_tranche():
    s = Stream()
    s.approve()
    first = s.arm_and_propose(1)
    day1 = proposal(
        first,
        [rung(1, "expired", filled="3")],
        [fill(11, 1, "3", "100000", date(2026, 11, 23))],
    )
    one = project(s.state, facts([day1]))
    assert names(one) == ["tranche_partially_filled"]
    event = one.events[0]
    assert event.fields["fill_amount"] == D("300000")
    assert event.fields["fill_quantity"] == D("3")
    assert "first_fill_amount" not in event.fields  # no base price before completion
    apply(s, one)
    assert s.state.tranche(1).completed is False  # a partial fill is not completion

    second = s.arm_and_propose(1)
    day2 = proposal(
        second,
        [rung(2, "filled", quantity="6", limit="98200", filled="6")],
        [fill(12, 2, "6", "98100", date(2026, 11, 24))],
    )
    two = project(s.state, facts([day1, day2]))
    assert names(two) == ["tranche_filled", "first_tranche_settled"]
    done = two.events[0]
    assert done.fields["fill_amount"] == D("588600")
    # 888,600 / 94.5% of 940,000: below 95%, but the remainder 51,400 < one share 98,200
    assert done.fields["first_fill_amount"] == D("888600")
    assert done.fields["first_fill_quantity"] == D("9")
    assert done.evidence["trade_date"] == "2026-11-24"
    assert two.final_state.authority == "active"


def test_a_fill_below_95_percent_with_a_remainder_above_one_share_stays_partial():
    out = tranche_event_for(
        _outcome("300000", "3", "100000"),
        tranche_amount=D("940000"),
        prior_amount=D("0"),
    )
    assert out == "tranche_partially_filled"


def test_95_percent_of_the_tranche_completes_it_even_when_one_share_remains():
    out = tranche_event_for(
        _outcome("893000", "9", "10000"),
        tranche_amount=D("940000"),
        prior_amount=D("0"),
    )
    assert out == "tranche_filled"
    just_below = tranche_event_for(
        _outcome("892999", "9", "10000"),
        tranche_amount=D("940000"),
        prior_amount=D("0"),
    )
    assert just_below == "tranche_partially_filled"


def test_without_a_limit_price_only_the_95_percent_rule_completes():
    out = tranche_event_for(
        _outcome("888600", "9", None), tranche_amount=D("940000"), prior_amount=D("0")
    )
    assert out == "tranche_partially_filled"


def _outcome(amount: str, qty: str, one_share: str | None):
    from app.services.mandate.projection_rules import ProposalOutcome

    return ProposalOutcome(
        fill_amount=D(amount),
        fill_quantity=D(qty),
        ledger_ids=(1,),
        trade_date=date(2026, 11, 23),
        one_share_value=None if one_share is None else D(one_share),
    )


# ----------------------------------------------------------------- I10
def test_i10_a_filled_rung_with_no_authoritative_ledger_row_writes_nothing():
    """Websocket-only rows never reach the facts: the rung says filled, the ledger does not."""
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(
        s.state,
        facts([proposal(pid, [rung(1, "filled", filled="10")], [])]),
    )
    assert plan.events == ()
    assert ProjectionHold.FILL_NOT_BOOKED.value in codes(plan)


def test_i10_a_booked_quantity_below_the_rungs_reported_fill_writes_nothing():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(
        s.state,
        facts(
            [
                proposal(
                    pid,
                    [rung(1, "filled", filled="10")],
                    [fill(11, 1, "4", "100000")],
                )
            ]
        ),
    )
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.FILL_NOT_BOOKED.value]


def test_i10_a_full_rung_without_a_reported_quantity_needs_the_whole_order_booked():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(
        s.state,
        facts(
            [
                proposal(
                    pid,
                    [rung(1, "filled", quantity="10", filled=None)],
                    [fill(11, 1, "10", "93000")],
                )
            ]
        ),
    )
    assert names(plan) == ["tranche_filled", "first_tranche_settled"]


@pytest.mark.parametrize(
    "state", ["resting", "acked", "partially_filled", "submitting"]
)
def test_i10_an_open_rung_writes_nothing(state: str):
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(
        s.state,
        facts([proposal(pid, [rung(1, state)], [fill(11, 1, "3", "100000")])]),
    )
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.RUNG_OPEN.value]


def test_i10_an_unverified_rung_writes_nothing():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(s.state, facts([proposal(pid, [rung(1, "unverified")])]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.OUTCOME_UNKNOWN.value]


def test_i10_a_fill_the_attribution_could_not_tie_to_the_rung_writes_nothing():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    fact = ProposalFact(
        proposal_id=pid,
        rungs=(rung(1, "expired"),),
        unattributed_rung_ids=frozenset({1}),
    )
    plan = project(s.state, facts([fact]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.FILL_ATTRIBUTION_UNKNOWN.value]


def test_i10_a_proposal_that_ended_without_any_fill_writes_tranche_unfilled():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(s.state, facts([proposal(pid, [rung(1, "expired")])]))
    assert names(plan) == ["tranche_unfilled"]
    assert "fill_amount" not in plan.events[0].fields
    assert plan.final_state.tranche(1).completed is False


def test_i10_a_voided_unsent_rung_is_a_zero_fill_proof():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    plan = project(
        s.state,
        facts([proposal(pid, [rung(1, "voided", order_id=None, key=None)])]),
    )
    assert names(plan) == ["tranche_unfilled"]


def test_i10_a_voided_local_stale_rung_that_was_sent_is_unknown_not_unfilled():
    assert rung_status(rung(1, "voided_local_stale")) == "unknown"
    assert (
        rung_status(rung(1, "voided_local_stale", order_id=None, key=None)) == "settled"
    )


def test_i10_an_open_fence_with_no_broker_number_is_not_a_zero_fill():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    unsent = proposal(pid, [rung(1, "voided", order_id=None, key="idem")])
    blocked = project(s.state, facts([unsent]))
    assert blocked.events == ()
    assert codes(blocked) == [ProjectionHold.FENCE_UNCLOSED.value]
    # with a broker number the order is known and its ending is evidence
    sent = proposal(pid, [rung(1, "expired", order_id="ORD7")])
    assert names(project(s.state, facts([sent]))) == ["tranche_unfilled"]


def test_i10_a_released_fence_with_a_voided_rung_closes_as_unfilled():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    seq = s.fence(1, pid)
    s.add("tranche_send_released", tranche_index=1, fenced_seq=seq)
    plan = project(
        s.state,
        facts([proposal(pid, [rung(1, "voided", order_id=None, key=None)])]),
    )
    assert names(plan) == ["tranche_unfilled"]


def test_i10_a_projection_replayed_on_its_own_output_writes_nothing_more():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    fact = proposal(
        pid,
        [rung(1, "filled", quantity="10", filled="10")],
        [fill(11, 1, "10", "94000")],
    )
    first = project(s.state, facts([fact]))
    assert names(first) == ["tranche_filled", "first_tranche_settled"]
    apply(s, first)
    again = project(s.state, facts([fact]))
    assert again.events == ()
    assert again.holds == ()


def test_i10_a_late_fill_after_the_terminal_event_blocks_cleanup_and_is_reported():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.add("tranche_unfilled", tranche_index=1, proposal_id=pid)
    s.add("retired")
    late = proposal(pid, [rung(1, "expired", filled="3")], [fill(11, 1, "3", "100000")])
    plan = project(s.state, facts([late], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert plan.events == ()
    assert ProjectionHold.RECORDED_FILL_MISMATCH.value in codes(plan)


# ----------------------------------------------------------------- authority steps
def _through_tranche_two() -> Stream:
    s = Stream()
    s.approve()
    s.fill_first("888600", "9")
    s.add("first_tranche_settled")
    second = s.arm_and_propose(2)
    s.add(
        "tranche_filled",
        tranche_index=2,
        proposal_id=second,
        fill_amount=D("700000"),
        fill_quantity=D("9"),
        evidence={"trade_date": "2026-12-28"},
    )
    return s


def test_the_third_tranche_completing_writes_exhausted():
    s = _through_tranche_two()
    third = s.arm_and_propose(3)
    closed = [
        proposal(
            s.proposals[1],
            [rung(1, "filled", filled="9")],
            [fill(11, 1, "9", str(Decimal("888600") / 9))],
        ),
        proposal(
            s.proposals[2],
            [rung(2, "filled", filled="9")],
            [fill(12, 2, "9", str(Decimal("700000") / 9))],
        ),
    ]
    last = proposal(
        third,
        [rung(3, "filled", quantity="9", filled="9")],
        [fill(13, 3, "9", "78000")],
    )
    plan = project(s.state, facts([*closed, last]))
    assert names(plan) == ["tranche_filled", "exhausted"]
    assert plan.final_state.authority == "exhausted"


# ----------------------------------------------------------------- thesis_broken -> closed
def _thesis_broken_plan(*, open_second: bool = False) -> tuple[Stream, UUID]:
    s = Stream()
    s.approve()
    pid = s.fill_first("940000", "10")
    s.add("first_tranche_settled")
    if open_second:
        s.arm_and_propose(2)
    s.add("thesis_broken")
    return s, pid


def _filled_first(pid: UUID) -> ProposalFact:
    return proposal(
        pid,
        [rung(1, "filled", filled="10")],
        [fill(11, 1, "10", "94000")],
    )


def test_closed_needs_orders_settled_and_the_plan_accounts_zero_reading():
    s, pid = _thesis_broken_plan()
    plan = project(s.state, facts([_filled_first(pid)], [reading(PLAN_ACCT)]))
    assert names(plan) == ["closed", "orders_settled"]
    closed = plan.events[0]
    assert closed.evidence["holdings_account_refs"] == [PLAN_ACCT]
    assert closed.evidence["holdings_quantity"] == "0"
    # the other scope account was never read: no disposal, no release
    assert ProjectionHold.SCOPE_ACCOUNT_UNREAD.value in codes(plan)
    assert "released" not in names(plan)


def test_closed_is_refused_while_the_plan_account_still_holds_the_symbol():
    s, pid = _thesis_broken_plan()
    plan = project(s.state, facts([_filled_first(pid)], [reading(PLAN_ACCT, "3")]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.HOLDINGS_REMAIN.value]


def test_closed_is_refused_without_a_holdings_reading():
    s, pid = _thesis_broken_plan()
    plan = project(s.state, facts([_filled_first(pid)], []))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.SCOPE_ACCOUNT_UNREAD.value]


def test_closed_is_refused_while_a_buy_order_is_still_open():
    """An unsettled buy can fill after a zero reading: the plan is not closed."""
    s, pid = _thesis_broken_plan(open_second=True)
    second = s.state.tranche(2).open_proposals[0]
    open_buy = proposal(second, [rung(2, "resting")])
    plan = project(s.state, facts([_filled_first(pid), open_buy], [reading(PLAN_ACCT)]))
    assert plan.events == ()
    assert ProjectionHold.RUNG_OPEN.value in codes(plan)


def test_closed_is_refused_on_a_reading_older_than_the_last_fill():
    s, pid = _thesis_broken_plan()
    old = reading(PLAN_ACCT, at=datetime(2026, 11, 23, 0, 30, tzinfo=UTC))
    plan = project(s.state, facts([_filled_first(pid)], [old]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.HOLDINGS_STALE.value]


def test_closed_is_refused_on_a_reading_taken_before_the_last_fill_though_still_fresh():
    """Inside the age limit, but older than the fill: the zero read pre-dates the shares."""
    s, pid = _thesis_broken_plan()
    late_fill = proposal(
        pid,
        [rung(1, "filled", filled="10")],
        [fill(11, 1, "10", "94000", date(2026, 11, 27))],  # filled 2026-11-27 01:00Z
    )
    before = reading(
        PLAN_ACCT, at=datetime(2026, 11, 27, 0, 30, tzinfo=UTC)
    )  # 5.5h old
    plan = project(s.state, facts([late_fill], [before]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.HOLDINGS_STALE.value]
    after = reading(PLAN_ACCT, at=datetime(2026, 11, 27, 1, 30, tzinfo=UTC))
    assert names(project(s.state, facts([late_fill], [after])))[0] == "closed"


def test_a_thesis_broken_plan_is_never_cleaned_up_without_closed_or_retired():
    s, pid = _thesis_broken_plan()
    plan = project(
        s.state,
        facts([_filled_first(pid)], [reading(PLAN_ACCT, "5"), reading(OTHER_ACCT)]),
    )
    assert plan.events == ()
    assert plan.final_state.cleanup == "none"


# ----------------------------------------------------------------- I19 cleanup
def _retired_plan() -> Stream:
    s = Stream()
    s.approve()
    s.add("retired")
    return s


def test_i19_a_zero_read_of_every_scope_account_releases_a_retired_plan():
    s = _retired_plan()
    plan = project(s.state, facts([], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert names(plan) == ["orders_settled", "holdings_disposed", "released"]
    disposed = plan.events[1].evidence
    assert disposed["holdings_disposition"] == "zero_holdings"
    assert disposed["holdings_account_refs"] == sorted([PLAN_ACCT, OTHER_ACCT])
    assert plan.final_state.released


def test_i19_a_scope_account_that_was_not_read_blocks_the_disposal():
    s = _retired_plan()
    plan = project(s.state, facts([], [reading(PLAN_ACCT)]))
    assert names(plan) == ["orders_settled"]
    assert codes(plan) == [ProjectionHold.SCOPE_ACCOUNT_UNREAD.value]
    assert plan.final_state.cleanup == "orders_clear"


def test_i19_a_reading_of_an_account_that_replaced_a_scope_account_proves_nothing():
    """The configured account changed after registration: the new account's zero is not
    the registered account's zero."""
    s = _retired_plan()
    swapped = [reading(PLAN_ACCT), reading("acct-replacement")]
    plan = project(s.state, facts([], swapped))
    assert "holdings_disposed" not in names(plan)
    assert "released" not in names(plan)
    assert codes(plan) == [ProjectionHold.SCOPE_ACCOUNT_UNREAD.value]


def test_i19_holdings_left_in_any_scope_account_block_the_disposal():
    s = _retired_plan()
    plan = project(s.state, facts([], [reading(PLAN_ACCT), reading(OTHER_ACCT, "1")]))
    assert names(plan) == ["orders_settled"]
    assert codes(plan) == [ProjectionHold.HOLDINGS_REMAIN.value]


def test_i19_a_stale_or_future_reading_is_not_a_zero_read():
    s = _retired_plan()
    stale = reading(OTHER_ACCT, at=NOW - timedelta(hours=7))
    assert "holdings_disposed" not in names(
        project(s.state, facts([], [reading(PLAN_ACCT), stale]))
    )
    future = reading(OTHER_ACCT, at=NOW + timedelta(hours=1))
    assert "holdings_disposed" not in names(
        project(s.state, facts([], [reading(PLAN_ACCT), future]))
    )


def test_i19_two_conflicting_newest_readings_are_unknown():
    s = _retired_plan()
    at = NOW - timedelta(minutes=5)
    plan = project(
        s.state,
        facts(
            [],
            [
                reading(PLAN_ACCT),
                reading(OTHER_ACCT, "0", at=at),
                reading(OTHER_ACCT, "2", at=at),
            ],
        ),
    )
    assert names(plan) == ["orders_settled"]
    assert codes(plan) == [ProjectionHold.HOLDINGS_UNKNOWN.value]


def test_i19_a_reading_of_another_symbol_proves_nothing():
    s = _retired_plan()
    wrong = [reading(PLAN_ACCT, symbol="000660"), reading(OTHER_ACCT, symbol="000660")]
    plan = project(s.state, facts([], wrong))
    assert names(plan) == ["orders_settled"]
    assert codes(plan) == [ProjectionHold.SCOPE_ACCOUNT_UNREAD.value]


@pytest.mark.parametrize(
    "open_rung",
    [rung(9, "resting"), rung(9, "unverified"), rung(9, "approved")],
)
def test_i19_an_unsettled_bound_order_blocks_orders_settled(open_rung: RungFact):
    s = _retired_plan()
    pid = uuid4()  # bound by column, never recorded in the stream
    plan = project(
        s.state,
        facts([proposal(pid, [open_rung])], [reading(PLAN_ACCT), reading(OTHER_ACCT)]),
    )
    assert plan.events == ()


def test_i19_a_sent_proposal_missing_from_the_stream_blocks_orders_settled():
    s = _retired_plan()
    stray = proposal(uuid4(), [rung(9, "expired")])
    plan = project(s.state, facts([stray], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert plan.events == ()
    assert codes(plan) == [ProjectionHold.BOUND_PROPOSAL_NOT_IN_STREAM.value]


def test_i19_an_unsent_proposal_missing_from_the_stream_does_not_block():
    s = _retired_plan()
    stray = proposal(uuid4(), [rung(9, "voided", order_id=None, key=None)])
    plan = project(s.state, facts([stray], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert names(plan) == ["orders_settled", "holdings_disposed", "released"]


def test_i19_an_unclosed_send_fence_blocks_orders_settled():
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    s.add("retired")
    sent = proposal(pid, [rung(1, "voided", order_id=None, key="idem")])
    plan = project(s.state, facts([sent], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert plan.events == ()
    assert ProjectionHold.FENCE_UNCLOSED.value in codes(plan)


def test_i19_a_never_approved_plan_is_released_in_one_pass_without_a_holdings_read():
    s = Stream()
    s.add("proposed")
    s.add("rejected")
    plan = project(s.state, facts())
    assert names(plan) == ["orders_settled", "holdings_disposed", "released"]
    assert plan.events[1].evidence == {"holdings_disposition": "never_adopted"}


def test_i19_a_plan_retired_before_approval_is_never_adopted_too():
    s = Stream()
    s.add("proposed")
    s.add("retired")
    plan = project(s.state, facts())
    assert plan.events[1].evidence == {"holdings_disposition": "never_adopted"}


def test_i19_an_approved_then_retired_plan_never_gets_never_adopted():
    s = _retired_plan()
    plan = project(s.state, facts())
    # approved once: the orders settle, but a holdings read is required to go on
    assert names(plan) == ["orders_settled"]
    assert codes(plan) == [ProjectionHold.SCOPE_ACCOUNT_UNREAD.value]


def test_i19_an_exhausted_plan_is_cleaned_up_only_after_a_scope_wide_zero_read():
    s = _through_tranche_two()
    pid3 = s.arm_and_propose(3)
    s.add(
        "tranche_filled",
        tranche_index=3,
        proposal_id=pid3,
        fill_amount=D("700000"),
        fill_quantity=D("9"),
        evidence={"trade_date": "2027-01-28"},
    )
    s.add("exhausted")
    closed = [
        proposal(
            s.proposals[1],
            [rung(1, "filled", filled="9")],
            [fill(11, 1, "9", str(Decimal("888600") / 9))],
        ),
        proposal(
            s.proposals[2],
            [rung(2, "filled", filled="9")],
            [fill(12, 2, "9", str(Decimal("700000") / 9))],
        ),
        proposal(
            pid3,
            [rung(3, "filled", filled="9")],
            [fill(13, 3, "9", str(Decimal("700000") / 9))],
        ),
    ]
    holding = project(
        s.state, facts(closed, [reading(PLAN_ACCT, "9"), reading(OTHER_ACCT)])
    )
    assert holding.events == ()  # shares held by design: not cleaned up
    assert codes(holding) == [ProjectionHold.HOLDINGS_REMAIN.value]
    flat = project(
        s.state,
        facts(
            closed,
            [
                reading(PLAN_ACCT, at=NOW),
                reading(OTHER_ACCT, at=NOW),
            ],
            now=NOW + timedelta(minutes=1),
        ),
    )
    assert names(flat) == ["orders_settled", "holdings_disposed", "released"]


def test_a_released_plan_takes_no_projection():
    s = _retired_plan()
    plan = project(s.state, facts([], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    apply(s, plan)
    assert s.state.released
    again = project(s.state, facts([], [reading(PLAN_ACCT), reading(OTHER_ACCT)]))
    assert again.events == ()


def test_an_empty_stream_projects_nothing():
    assert project(PlanState(), facts()).events == ()


def test_every_projected_event_is_accepted_by_the_fold_in_order():
    """``project`` applies each event to the fold before returning it."""
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    fact = proposal(
        pid,
        [rung(1, "filled", quantity="10", filled="10")],
        [fill(11, 1, "10", "94000")],
    )
    plan = project(s.state, facts([fact]))
    apply(s, plan)
    assert s.state.authority == plan.final_state.authority == "active"
    assert s.state.effective_session == SESSION
