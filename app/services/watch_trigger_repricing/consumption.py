"""ROB-1286 §3 — the canonical "is this watch event consumed?" definition.

Why this module exists
----------------------
``review.investment_watch_events`` ships **no consumption column**. Its
mutable fields are ``outcome``, ``delivery_status`` and
``follow_up_report_item_id``, and none of them mean "a session has taken
responsibility for re-judging this fire":

* ``outcome`` is the scanner's classification at fire time
  (``review_required`` is the *input* to this flow, not its output).
* ``delivery_status`` tracks Hermes delivery, not downstream judgement.
* ``follow_up_report_item_id`` is already owned by ROB-405 Slice E
  (``app/services/trade_journal/watch_follow_up_service.py`` scans for
  ``follow_up_report_item_id IS NULL`` to build mock-loop retrospectives).
  Reusing it here would make each feature's writes look like the other's
  work and silently starve both scans.

So consumption is a **claim keyed by ``event_uuid``**, held in the claim
store (:mod:`.claims`), and this module is the only place that turns a
claim into a verdict. Both consumers are defined against it:

``A안`` (this flow)
    Spawns only on :data:`ConsumptionState.UNCLAIMED`.
``B안`` (rep session end-of-session re-check, operator repo
    ``prompts/kr-open-trade.md`` step 5)
    Re-judges anything it sees as unconsumed. It reads
    ``investment_watch_events_list_recent``, so the same verdict reaches it
    via :func:`project_claim_state` once that read surface is wired.

Three-valued on purpose
-----------------------
``UNKNOWN`` is not ``UNCLAIMED``. If the claim store cannot answer, we do
not know whether another consumer is already working the event, and
guessing "unclaimed" is how one fire becomes two sell proposals. Callers
fail closed via :func:`may_consume`; the fire is not lost, because B안
still re-checks it at session end.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CONSUMABLE_OUTCOMES",
    "ConsumptionState",
    "is_consumable_outcome",
    "may_consume",
    "project_claim_state",
]

# Only a fire the scanner marked as needing review is a repricing candidate.
# ``notified`` / ``preview_attached`` / ``executed`` fires are other lanes'
# business; widening this set widens what reaches order_proposal_create.
CONSUMABLE_OUTCOMES = frozenset({"review_required"})


class ConsumptionState(StrEnum):
    """Whether some consumer has taken responsibility for an event."""

    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    UNKNOWN = "unknown"


def is_consumable_outcome(outcome: str | None) -> bool:
    """True iff ``outcome`` is a fire this flow is allowed to act on."""
    return outcome in CONSUMABLE_OUTCOMES


def project_claim_state(
    *,
    claim_found: bool,
    store_available: bool,
) -> ConsumptionState:
    """Map raw claim-store output onto the canonical verdict.

    This is the single conversion both consumers share. ``store_available``
    is checked first: an unreachable store cannot prove absence, and
    "lookup returned nothing" must never be read as "nobody owns it".
    """
    if not store_available:
        return ConsumptionState.UNKNOWN
    return ConsumptionState.CLAIMED if claim_found else ConsumptionState.UNCLAIMED


def may_consume(state: ConsumptionState) -> bool:
    """True iff a consumer may take this event.

    Fail-closed: only a positively proven ``UNCLAIMED`` clears.
    """
    return state is ConsumptionState.UNCLAIMED
