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

Five-valued on purpose (r2)
---------------------------
r1 had three states and that was the root of BLOCKER-1. With only
``CLAIMED``/``UNCLAIMED``, "a session ran and finished" and "a tick died
holding the lease" are the *same* state, so the lease expiry that rescues
the second necessarily resurrects the first: 30 minutes after a successful
spawn the event read as ``UNCLAIMED`` again and was re-spawned. Success and
failure must be different states before an expiry rule can tell them apart.

``UNCLAIMED``
    Nobody owns it. The only state a consumer may take.
``CLAIMED``
    An in-progress lease. Expires, because the holder may have crashed --
    that expiry is what stops a dead tick from burying the fire.
``CONSUMED``
    Terminal. A session was *proven* started for this event. Never expires,
    never reclaimable: the work happened, and repeating it would put a
    second sell proposal on one fire.
``QUARANTINED``
    Terminal, and a fault. The spawn result was ambiguous and could not be
    reconciled, so we do not know whether a session started. Blocks
    re-spawn (guessing "it didn't" is how one fire becomes two proposals)
    and is reported for operator reconciliation rather than left silent --
    see :mod:`.orchestrator` for the cost this trade accepts.
``UNKNOWN``
    The store could not answer. Not the same as ``UNCLAIMED``: an
    unreachable store cannot prove absence, and guessing is how one fire
    becomes two sell proposals. Callers fail closed via :func:`may_consume`;
    the fire is not lost, because B안 still re-checks it at session end.

Note that event-level consumption and per-symbol concurrency are different
clocks and must stay that way. ``CONSUMED`` is permanent (this *event* is
done forever) while the symbol it touched is only busy for the lease (a
*later* fire on the same symbol must not be blocked for all day).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CONSUMABLE_OUTCOMES",
    "TERMINAL_STATES",
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
    CONSUMED = "consumed"
    QUARANTINED = "quarantined"
    UNKNOWN = "unknown"


# States a lease expiry must never walk back. Both mean a session may be
# running or may have run; re-spawning either duplicates a fire.
TERMINAL_STATES = frozenset({ConsumptionState.CONSUMED, ConsumptionState.QUARANTINED})


def is_consumable_outcome(outcome: str | None) -> bool:
    """True iff ``outcome`` is a fire this flow is allowed to act on."""
    return outcome in CONSUMABLE_OUTCOMES


def project_claim_state(
    *,
    claim_found: bool,
    store_available: bool,
    terminal_state: ConsumptionState | None = None,
) -> ConsumptionState:
    """Map raw claim-store output onto the canonical verdict.

    This is the single conversion both consumers share.

    ``store_available`` is checked first: an unreachable store cannot prove
    absence, and "lookup returned nothing" must never be read as "nobody
    owns it".

    ``terminal_state`` is checked *before* ``claim_found``, because
    ``claim_found`` carries the lease clock and a terminal record outlives
    its lease by design. Reading a terminal record through the lease would
    reintroduce BLOCKER-1 exactly.
    """
    if not store_available:
        return ConsumptionState.UNKNOWN
    if terminal_state is not None:
        if terminal_state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal state: {terminal_state!r}")
        return terminal_state
    return ConsumptionState.CLAIMED if claim_found else ConsumptionState.UNCLAIMED


def may_consume(state: ConsumptionState) -> bool:
    """True iff a consumer may take this event.

    Fail-closed: only a positively proven ``UNCLAIMED`` clears.
    """
    return state is ConsumptionState.UNCLAIMED
