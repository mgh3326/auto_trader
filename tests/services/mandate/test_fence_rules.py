"""Pure send-fence decisions (hk 3795 T4c): I9, I11, I15, I16, I24 and the automatic
qualification of §4.6 "자격의 재확인". No database; names carry the invariant."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services.mandate import vocabulary as v
from app.services.mandate.fence_rules import (
    BLOCKING,
    DEMOTING,
    AutoFacts,
    SendFacts,
    auto_refusal,
    is_blocking,
    plan_fence_exposure,
    send_refusal,
)
from tests.services.mandate.support import SESSION, D, Stream

pytestmark = pytest.mark.unit
R = v.MandateRefusal


def _facts(stream: Stream | None = None, pid=None, **over) -> SendFacts:
    s = stream or Stream()
    if stream is None:
        s.approve()
        pid = s.arm_and_propose(1)
    base = {
        "state": s.state,
        "tranche_index": 1,
        "proposal_id": pid,
        "kst_day": SESSION,
        "notional": D("940000"),
        "planned_amount": D("2350000"),
        "adopted_amount": D("0"),
        "integrity_ok": True,
        "identity_ok": True,
        "account_binding_ok": True,
        "actual_account_ref": "acct-1",
        "plan_account_ref": "acct-1",
        "frozen": False,
    }
    base.update(over)
    return SendFacts(**base)


def test_a_healthy_bound_send_is_not_refused() -> None:
    assert send_refusal(_facts()) is None


def test_i15_a_proposal_that_is_not_the_plans_binding_is_blocked() -> None:
    assert send_refusal(_facts(identity_ok=False)) == R.BINDING_MISMATCH.value


def test_i21_a_broken_clause_or_approval_record_is_blocked() -> None:
    assert send_refusal(_facts(integrity_ok=False)) == R.BINDING_BROKEN.value


@pytest.mark.parametrize(
    "actual",
    [None, "acct-2"],
    ids=["cannot-compute", "another-account"],
)
def test_i15_the_account_the_request_will_use_must_be_the_plans(actual) -> None:
    assert send_refusal(_facts(actual_account_ref=actual)) == R.ACCOUNT_MISMATCH.value


def test_i15_a_plan_account_that_is_not_the_clause_account_is_blocked() -> None:
    assert send_refusal(_facts(account_binding_ok=False)) == R.ACCOUNT_MISMATCH.value


@pytest.mark.parametrize("event", ["suspended", "retired", "thesis_broken"])
def test_i16_a_plan_without_sending_authority_has_no_fence(event: str) -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.add(
        event,
        **(
            {"evidence": {"retrospective_id": 1, "approval_issue_id": "x"}}
            if event == "thesis_broken"
            else {}
        ),
    )
    assert send_refusal(_facts(s, pid)) == R.PLAN_NOT_ACTIVE.value


def test_i16_a_fence_before_the_effective_session_is_refused() -> None:
    assert send_refusal(_facts(kst_day=date(2026, 11, 20))) == R.PLAN_NOT_ACTIVE.value


def test_i24_an_unclosed_fence_blocks_the_next_fence_of_the_tranche() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    s.fence(1, pid)
    assert send_refusal(_facts(s, pid)) == R.TRANCHE_UNCERTAIN.value


def test_i24_a_closed_fence_does_not_block_a_new_attempt() -> None:
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    seq = s.fence(1, pid)
    s.add("tranche_send_released", tranche_index=1, fenced_seq=seq)
    assert send_refusal(_facts(s, pid)) is None


def test_i9_a_proposal_that_is_not_the_one_open_proposal_is_a_duplicate() -> None:
    s = Stream()
    s.approve()
    s.arm_and_propose(1)
    assert send_refusal(_facts(s, uuid4())) == R.TRANCHE_DUPLICATE.value


def test_i8_the_whole_plan_amount_caps_the_send() -> None:
    assert send_refusal(_facts(notional=D("2350001"))) == R.PLAN_AMOUNT_EXCEEDED.value
    # the adopted amount is not new money
    assert (
        send_refusal(_facts(notional=D("940000"), adopted_amount=D("1500000")))
        == R.PLAN_AMOUNT_EXCEEDED.value
    )


def test_i8_the_tranche_remainder_alone_is_a_demotion_not_a_block() -> None:
    """A person who sees the figures may approve above the tranche remainder (§4.7)."""
    assert send_refusal(_facts(notional=D("1000000"))) is None


def test_i11_a_freeze_or_an_unreadable_freeze_is_blocked() -> None:
    assert send_refusal(_facts(frozen=True)) == R.DRAWDOWN_FROZEN.value
    assert send_refusal(_facts(frozen=None)) == R.UNREADABLE.value


def test_every_blocking_code_is_blocking_and_every_demotion_is_not() -> None:
    assert BLOCKING.isdisjoint(DEMOTING)
    assert all(is_blocking(code) for code in BLOCKING)
    assert not any(is_blocking(code) for code in DEMOTING)
    assert is_blocking("a_code_nobody_classified")  # unknown refuses (fail closed)


def _auto(**over) -> AutoFacts:
    base = {
        "switch_enabled": True,
        "sleeve_live": True,
        "live_terms_id": 5,
        "live_terms_expired": False,
        "plan_terms_id": 5,
        "judged_terms_id": 5,
        "profile_id": 9,
        "profile_terms_id": 5,
        "profile_max_tranche_index": 1,
        "profile_effective_ok": True,
        "profile_expired": False,
        "profile_approval_ok": True,
        "tranche_index": 1,
    }
    base.update(over)
    return AutoFacts(**base)


def test_x5_a_withdrawn_profile_makes_the_automatic_fence_a_demotion() -> None:
    """The latest profile (max_tranche_index 0) is the revocation (r4618309267)."""
    assert auto_refusal(_auto()) is None
    revoked = _auto(profile_id=10, profile_max_tranche_index=0)
    assert auto_refusal(revoked) == R.ACTIVATION_MISSING.value


def test_x16_a_newer_profile_of_another_sleeve_is_not_read_here() -> None:
    """Facts carry the plan sleeve's own latest profile; its fields alone decide."""
    assert auto_refusal(_auto(profile_id=3, profile_max_tranche_index=2)) is None


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"switch_enabled": False}, R.SWITCH_OFF.value),
        ({"sleeve_live": False}, "sleeve_not_live"),
        ({"live_terms_id": None}, R.TERMS_MISSING.value),
        ({"live_terms_expired": True}, R.TERMS_EXPIRED.value),
        ({"plan_terms_id": 4}, R.TERMS_NOT_CURRENT.value),
        ({"judged_terms_id": 4}, R.TERMS_NOT_CURRENT.value),
        ({"profile_id": None}, R.ACTIVATION_MISSING.value),
        ({"profile_terms_id": 4}, R.ACTIVATION_MISSING.value),
        ({"profile_expired": True}, R.ACTIVATION_MISSING.value),
        ({"profile_effective_ok": False}, R.ACTIVATION_MISSING.value),
        ({"profile_approval_ok": False}, R.BINDING_BROKEN.value),
        ({"tranche_index": 2}, R.TRANCHE_NOT_ACTIVATED.value),
    ],
)
def test_x8_each_lost_qualification_stops_the_automatic_fence(over, code) -> None:
    assert auto_refusal(_auto(**over)) == code


def test_x8_only_a_broken_approval_record_is_a_block_the_rest_demote() -> None:
    for over in (
        {"switch_enabled": False},
        {"sleeve_live": False},
        {"plan_terms_id": 4},
    ):
        code = auto_refusal(_auto(**over))
        assert code is not None and not is_blocking(code)
    broken = auto_refusal(_auto(profile_approval_ok=False))
    assert broken is not None and is_blocking(broken)


def test_the_exposure_of_a_plan_survives_a_fill_but_not_a_release() -> None:
    """X26 (human side): a fill recorded after a callback rollback stays exposure."""
    s = Stream()
    s.approve()
    pid = s.arm_and_propose(1)
    seq = s.fence(1, pid)
    assert plan_fence_exposure(s.state) == D("940000")  # unclosed fence
    s.add(
        "tranche_partially_filled",
        tranche_index=1,
        proposal_id=pid,
        fill_amount=D("300000"),
        fill_quantity=D("3"),
        evidence={"trade_date": "2026-11-23"},
    )
    assert seq not in s.state.unclosed_fences
    assert plan_fence_exposure(s.state) == D("300000")  # filled amount remains
    s2 = Stream()
    s2.approve()
    pid2 = s2.arm_and_propose(1)
    seq2 = s2.fence(1, pid2)
    s2.add("tranche_send_released", tranche_index=1, fenced_seq=seq2)
    assert plan_fence_exposure(s2.state) == Decimal(0)  # proven unsent


def test_the_rules_module_only_refuses_it_builds_no_grant() -> None:
    facts = _facts()
    assert replace(facts, frozen=True) != facts
    assert send_refusal(replace(facts, frozen=True)) is not None
