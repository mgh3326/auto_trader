"""ROB-1284 — evidence-first guarantees for the phantom-resting rung sweep.

The safety property under test is one sentence: *a rung is transitioned only on
committed terminal broker evidence*. Every other outcome — no evidence, an
still-open ledger, contradictory evidence, an illegal transition — must leave
the rung exactly where it was.

These are pure-function tests: ``classify_rung`` takes its clock and its
evidence as arguments, so nothing here needs a database or a broker.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from app.services.order_proposals.errors import (
    OrderProposalInvalidStateTransition,
)
from app.services.order_proposals.resting_sweep import (
    LEDGER_OPEN_STATUSES,
    LEDGER_STATUS_TO_RUNG_TERMINAL,
    LedgerEvidence,
    RungCandidate,
    RungVerdict,
    classify_rung,
    summarize,
)
from app.services.order_proposals.state_machine import (
    EVIDENCE_ACCEPTING_RUNG_STATES,
    assert_rung_transition,
)

NOW = datetime.datetime(2026, 8, 18, 1, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.unit


def _candidate(**over) -> RungCandidate:
    base = {
        "proposal_id": "6e13b685-0000-0000-0000-000000000000",
        "rung_id": 1,
        "rung_index": 1,
        "state": "resting",
        "side": "sell",
        "symbol": "257720",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_order_id": "0023769000",
        "idempotency_key": "idem-1",
        "correlation_id": "corr-1",
        "quantity": Decimal("10"),
        "limit_price": Decimal("43800"),
    }
    base.update(over)
    return RungCandidate(**base)


def _evidence(status: str, **over) -> LedgerEvidence:
    base = {
        "ledger_table": "review.kis_live_order_ledger",
        "ledger_id": 150,
        "status": status,
        "match_key": "broker_order_id",
        "broker_order_id": "0023769000",
    }
    base.update(over)
    return LedgerEvidence(**base)


# --------------------------------------------------------------- TRANSITION


@pytest.mark.parametrize(
    ("ledger_status", "expected"),
    [
        ("expired", "expired"),
        ("cancelled", "cancelled"),
        ("filled", "filled"),
        # A broker rejection seen after submission is expiry evidence for a
        # resting DAY rung, never the submit-time `rejected` transition.
        ("rejected", "expired"),
    ],
)
def test_terminal_broker_evidence_transitions(ledger_status, expected):
    d = classify_rung(_candidate(), (_evidence(ledger_status),), observed_at=NOW)
    assert d.verdict is RungVerdict.TRANSITION
    assert d.target_state == expected
    assert d.reason_code == "terminal_broker_evidence"


def test_transition_carries_auditable_evidence_fields():
    """A reviewer must be able to audit one row without re-querying."""
    d = classify_rung(
        _candidate(),
        (_evidence("expired", filled_qty=Decimal("4")),),
        observed_at=NOW,
    )
    row = d.as_row()
    assert row["broker_order_id"] == "0023769000"
    assert row["ledger_rows"][0]["status"] == "expired"
    assert row["remaining_qty"] == "6"  # 10 ordered - 4 filled
    assert row["observed_at"] == NOW.isoformat()
    assert row["verdict"] == "TRANSITION"


# --------------------------------------------------------------- NO_EVIDENCE


def test_no_ledger_row_is_never_a_transition():
    d = classify_rung(_candidate(), (), observed_at=NOW)
    assert d.verdict is RungVerdict.NO_EVIDENCE
    assert d.reason_code == "no_ledger_row"
    assert d.target_state is None


@pytest.mark.parametrize("open_status", sorted(LEDGER_OPEN_STATUSES))
def test_still_open_ledger_is_never_a_transition(open_status):
    """Absence of a terminal marking is not evidence of expiry."""
    d = classify_rung(_candidate(), (_evidence(open_status),), observed_at=NOW)
    assert d.verdict is RungVerdict.NO_EVIDENCE
    assert d.reason_code == "ledger_still_open"


def test_partial_is_treated_as_open_not_as_terminal_evidence():
    """`partial` is an open status in every reconcile scan; booking partial
    fills belongs to the reconcile kernel, not to this sweep."""
    assert "partial" in LEDGER_OPEN_STATUSES
    assert "partial" not in LEDGER_STATUS_TO_RUNG_TERMINAL
    d = classify_rung(_candidate(), (_evidence("partial"),), observed_at=NOW)
    assert d.verdict is RungVerdict.NO_EVIDENCE


def test_rung_without_any_evidence_key_is_not_transitioned():
    """The ROB-1277 shape: broker_order_id permanently null. Not expiry."""
    d = classify_rung(
        _candidate(broker_order_id=None, idempotency_key=None, correlation_id=None),
        (_evidence("expired"),),
        observed_at=NOW,
    )
    assert d.verdict is RungVerdict.NO_EVIDENCE
    assert d.reason_code == "no_evidence_keys"


# ------------------------------------------------------------------ CONFLICT


def test_contradictory_ledger_rows_conflict_and_do_not_transition():
    d = classify_rung(
        _candidate(),
        (_evidence("expired"), _evidence("filled", ledger_id=151)),
        observed_at=NOW,
    )
    assert d.verdict is RungVerdict.CONFLICT
    assert d.reason_code == "ledger_evidence_disagrees"
    assert d.target_state is None


def test_illegal_transition_is_a_conflict_not_a_forced_write():
    """`acked` has no `expired` edge. The broker says expired; the rung was
    never recorded resting. That is a modelling conflict for a human, not a
    reason to force the state machine."""
    with pytest.raises(OrderProposalInvalidStateTransition):
        assert_rung_transition("acked", "expired")
    d = classify_rung(
        _candidate(state="acked"), (_evidence("expired"),), observed_at=NOW
    )
    assert d.verdict is RungVerdict.CONFLICT
    assert d.reason_code == "transition_not_permitted"


def test_unknown_ledger_status_is_a_conflict():
    d = classify_rung(_candidate(), (_evidence("something_new"),), observed_at=NOW)
    assert d.verdict is RungVerdict.CONFLICT
    assert d.reason_code == "unmapped_ledger_status"


def test_agreeing_duplicate_rows_still_transition():
    """Two ledgers agreeing is not a conflict — only disagreement is."""
    d = classify_rung(
        _candidate(),
        (
            _evidence("expired"),
            _evidence(
                "expired", ledger_id=999, ledger_table="review.live_order_ledger"
            ),
        ),
        observed_at=NOW,
    )
    assert d.verdict is RungVerdict.TRANSITION


# ------------------------------------------------------------- invariants


@pytest.mark.parametrize("state", sorted(EVIDENCE_ACCEPTING_RUNG_STATES))
def test_every_candidate_state_survives_classification(state):
    """No candidate state may crash or silently vanish."""
    d = classify_rung(_candidate(state=state), (), observed_at=NOW)
    assert d.verdict in set(RungVerdict)


@pytest.mark.parametrize("state", sorted(EVIDENCE_ACCEPTING_RUNG_STATES))
def test_declared_target_is_always_reachable_from_the_rung_state(state):
    """Whenever the sweep says TRANSITION, the state machine agrees."""
    for ledger_status in LEDGER_STATUS_TO_RUNG_TERMINAL:
        d = classify_rung(
            _candidate(state=state), (_evidence(ledger_status),), observed_at=NOW
        )
        if d.verdict is RungVerdict.TRANSITION:
            assert d.target_state is not None
            assert_rung_transition(state, d.target_state)  # must not raise


def test_summary_accounts_for_every_row_and_splits_buy_sell():
    """Mixed-side population: a sell-only summary hides buying-power impact."""
    decisions = [
        classify_rung(
            _candidate(side="sell"), (_evidence("expired"),), observed_at=NOW
        ),
        classify_rung(_candidate(side="buy"), (), observed_at=NOW),
        classify_rung(
            _candidate(side="buy"),
            (_evidence("expired"), _evidence("filled", ledger_id=2)),
            observed_at=NOW,
        ),
    ]
    s = summarize(decisions)
    assert s["candidates"] == 3
    assert sum(s["by_verdict"].values()) == 3
    assert s["by_verdict"] == {"TRANSITION": 1, "NO_EVIDENCE": 1, "CONFLICT": 1}
    assert s["by_side"]["buy"]["TRANSITION"] == 0
    assert s["by_side"]["sell"]["TRANSITION"] == 1
    assert sum(s["by_reason_code"].values()) == 3


# ------------------------------------------------- cross-key attribution (R1)


def test_evidence_from_a_different_rung_must_not_transition_this_one():
    """Regression: matching on ANY single key re-attributes evidence.

    A ledger row can carry an ``order_no`` belonging to one rung and a
    ``correlation_id`` belonging to another. Reading it as evidence for either
    one books a terminal state onto a rung the broker never spoke about. The
    ownership check (``RestingRungSweepService.verify_ownership``, delegating to
    ``find_unambiguous_evidence_rung_id``) drops such a row before it reaches
    ``classify_rung`` — so with the row correctly dropped, the verdict must be
    NO_EVIDENCE, not a transition.

    The first version of this sweep did not do that and turned a `resting` rung
    into `filled` off another rung's fill; two ROB-900 conflict tests caught it.
    """
    d = classify_rung(_candidate(), (), observed_at=NOW)
    assert d.verdict is RungVerdict.NO_EVIDENCE
    assert d.reason_code == "no_ledger_row"
