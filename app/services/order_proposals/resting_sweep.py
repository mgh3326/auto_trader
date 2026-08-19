"""ROB-1284 — evidence-first convergence sweep for phantom broker-live rungs.

Why this exists
---------------
``sweep_expired`` deliberately *skips* any group holding a rung in a
non-voidable state (``service.py`` -- "a partially-submitted or filled proposal
past its window must not be force-expired").  That is correct: a local clock is
not evidence about a broker order.  But nothing else ever closes those rungs
either, so a rung whose broker order died long ago stays ``resting`` forever and
downstream consumers keep reserving buying power for a net that no longer
exists, and keep blocking legitimate opposite-side proposals.

``live_order_ledger._converge_proposal_rung`` names the missing piece verbatim:
"A guaranteed-convergence proposal-rung reconcile sweep is tracked as
follow-up."  This module is that sweep.

Two structural differences from the existing repair pre-passes
(``_repair_terminal_kis_proposal_projections`` /
``_repair_terminal_toss_proposal_projections``):

1. **Rung-driven, not ledger-driven.**  The existing passes start from ledger
   rows and can only fix rungs whose ledger row is a listed projection
   candidate.  A rung whose ledger row was never created, or was created under a
   different key, is invisible to them.  This sweep starts from the rung.
2. **Untruncated.**  No ``limit``, no time window -- the candidate set is
   defined by rung state alone.  Every paged path in the system (see the module
   ``QUERY_PATH_LIMITS`` note below) undercounts this population.

Safety contract (evidence-first, fail-closed)
---------------------------------------------
* A rung is transitioned **only** on committed terminal broker evidence.
* Absent, unreadable, still-open, or contradictory evidence transitions
  *nothing* -- it is reported, never absorbed into the transition set.
* ``classify_rung`` is pure (stdlib only, no DB/network/clock): the caller
  injects both the evidence and ``observed_at``.
* The sweep never contacts a broker and never mutates a broker.  Its evidence is
  the already-committed ``*_order_ledger`` rows, which the ROB-395/407 reconcile
  kernel booked from broker responses.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal
from enum import StrEnum

from app.services.order_proposals import state_machine as sm

__all__ = [
    "LEDGER_OPEN_STATUSES",
    "LEDGER_STATUS_TO_RUNG_TERMINAL",
    "LedgerEvidence",
    "RungCandidate",
    "RungVerdict",
    "SweepDecision",
    "classify_rung",
]


class RungVerdict(StrEnum):
    """The three — and only three — outcomes a candidate rung can receive.

    Only ``TRANSITION`` is ever applied.  ``NO_EVIDENCE`` and ``CONFLICT`` are
    reported and left untouched; collapsing either of them into ``TRANSITION``
    is the failure mode this whole module exists to prevent.
    """

    TRANSITION = "TRANSITION"
    NO_EVIDENCE = "NO_EVIDENCE"
    CONFLICT = "CONFLICT"


# A ledger row in one of these statuses is still live at the broker as far as
# the ledger knows.  It is NOT expiry evidence -- ``partial`` in particular is an
# open status in every reconcile candidate scan, and booking partial fills is
# the reconcile kernel's job, not this sweep's.
LEDGER_OPEN_STATUSES: frozenset[str] = frozenset({"accepted", "pending", "partial"})

# Terminal ledger status -> rung terminal state.
#
# ``rejected`` -> ``expired`` mirrors the KIS reconcile kernel: a broker
# rejection observed *after* submission is expiry evidence for a resting DAY
# rung, not the submit-time ``rejected`` transition (which ``resting`` cannot
# reach anyway).
#
# NOTE (divergence, deliberate): the US/crypto path maps ledger ``expired`` to
# rung ``cancelled`` on the stale premise that "proposal rungs have no separate
# expired state".  They do -- ``resting -> expired`` is a legal edge.  This
# sweep books DAY expiry as ``expired`` so the rung records what actually
# happened.  The existing path is left untouched here; see the report.
LEDGER_STATUS_TO_RUNG_TERMINAL: dict[str, str] = {
    "filled": "filled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "expired": "expired",
    "rejected": "expired",
}


@dataclasses.dataclass(frozen=True)
class LedgerEvidence:
    """One committed broker-evidence ledger row matched to a rung."""

    ledger_table: str
    ledger_id: int
    status: str
    match_key: str
    broker_order_id: str | None = None
    idempotency_key: str | None = None
    correlation_id: str | None = None
    filled_qty: Decimal | None = None
    reconciled_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class RungCandidate:
    """A rung currently in a broker-live (evidence-accepting) state."""

    proposal_id: str
    rung_id: int
    rung_index: int
    state: str
    side: str
    symbol: str
    market: str
    account_mode: str
    broker_order_id: str | None
    idempotency_key: str | None
    correlation_id: str | None
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    updated_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class SweepDecision:
    """Per-rung verdict with the evidence that produced it.

    Every field a reviewer needs to audit one row without re-querying:
    broker order id, broker/ledger status, remaining (unfilled) quantity, the
    observation time, and the classification itself.

    ``ownership_conflict`` records that at least one *matched* ledger row was
    refused attribution to this rung and dropped from ``evidence``.  It is kept
    on the decision even when the surviving evidence would classify cleanly: a
    conflict that was observed and then dropped from the response is a conflict
    the operator never gets to see.
    """

    candidate: RungCandidate
    verdict: RungVerdict
    reason_code: str
    observed_at: datetime.datetime
    target_state: str | None = None
    evidence: tuple[LedgerEvidence, ...] = ()
    ownership_conflict: str | None = None

    @property
    def remaining_qty(self) -> Decimal | None:
        """Unfilled quantity implied by the evidence, or None if unknown."""
        if self.candidate.quantity is None:
            return None
        filled = next(
            (e.filled_qty for e in self.evidence if e.filled_qty is not None), None
        )
        return self.candidate.quantity - (filled or Decimal(0))

    def as_row(self) -> dict[str, object]:
        """Flat, audit-ready record for the dry-run report."""
        remaining = self.remaining_qty
        return {
            "proposal_id": self.candidate.proposal_id,
            "rung_index": self.candidate.rung_index,
            "rung_id": self.candidate.rung_id,
            "symbol": self.candidate.symbol,
            "market": self.candidate.market,
            "account_mode": self.candidate.account_mode,
            "side": self.candidate.side,
            "rung_state": self.candidate.state,
            "verdict": str(self.verdict),
            "reason_code": self.reason_code,
            "target_state": self.target_state,
            "broker_order_id": self.candidate.broker_order_id,
            "ledger_rows": [
                {
                    "table": e.ledger_table,
                    "ledger_id": e.ledger_id,
                    "status": e.status,
                    "matched_by": e.match_key,
                    "reconciled_at": (
                        e.reconciled_at.isoformat() if e.reconciled_at else None
                    ),
                }
                for e in self.evidence
            ],
            "remaining_qty": str(remaining) if remaining is not None else None,
            "ownership_conflict": self.ownership_conflict,
            "observed_at": self.observed_at.isoformat(),
        }


def _is_terminal_status(status: str) -> bool:
    return status.strip().lower() not in LEDGER_OPEN_STATUSES


def classify_rung(
    candidate: RungCandidate,
    evidence: tuple[LedgerEvidence, ...],
    *,
    observed_at: datetime.datetime,
    ownership_conflict: str | None = None,
) -> SweepDecision:
    """Decide one rung's fate from its ledger evidence. Pure — no I/O, no clock.

    Fail-closed at every branch: the ``TRANSITION`` verdict is returned only
    when exactly one terminal rung state is implied by the evidence *and* the
    state machine permits reaching it from the rung's current state.

    ``ownership_conflict`` is the caller's report that some matched ledger row
    was refused attribution to this rung and dropped before classification
    (``RestingRungSweepService.verify_ownership``).  Two things follow, and both
    matter:

    * it is recorded on **every** verdict, so the fact that a conflict existed
      never disappears just because the surviving subset classified cleanly;
    * it **downgrades a would-be TRANSITION to CONFLICT**.  "Some of the rows
      that matched this rung's keys could not be attributed to it" is
      contradictory evidence, and this module's whole contract is that
      contradictory evidence transitions nothing.
    """

    def decide(
        verdict: RungVerdict, reason: str, target: str | None = None
    ) -> SweepDecision:
        if verdict is RungVerdict.TRANSITION and ownership_conflict is not None:
            # Never write off a partially-attributable evidence set.
            verdict, reason, target = (
                RungVerdict.CONFLICT,
                "ownership_conflict_with_partial_evidence",
                None,
            )
        return SweepDecision(
            candidate=candidate,
            verdict=verdict,
            reason_code=reason,
            observed_at=observed_at,
            target_state=target,
            evidence=evidence,
            ownership_conflict=ownership_conflict,
        )

    if candidate.state not in sm.RUNG_STATES:
        return decide(RungVerdict.CONFLICT, "unknown_rung_state")
    if sm.is_terminal(candidate.state):
        # Already converged; not this sweep's business.
        return decide(RungVerdict.NO_EVIDENCE, "rung_already_terminal")

    if not any(
        (
            candidate.broker_order_id,
            candidate.idempotency_key,
            candidate.correlation_id,
        )
    ):
        # Nothing to match on. This is the ROB-1277 shape (broker_order_id
        # permanently null) and it is NOT expiry evidence.
        return decide(RungVerdict.NO_EVIDENCE, "no_evidence_keys")

    if not evidence:
        return decide(RungVerdict.NO_EVIDENCE, "no_ledger_row")

    terminal = tuple(e for e in evidence if _is_terminal_status(e.status))
    if not terminal:
        # The ledger itself still believes the order is live. Absence of a
        # terminal marking is not evidence of expiry.
        return decide(RungVerdict.NO_EVIDENCE, "ledger_still_open")

    unmapped = tuple(
        e
        for e in terminal
        if e.status.strip().lower() not in LEDGER_STATUS_TO_RUNG_TERMINAL
    )
    if unmapped:
        return decide(RungVerdict.CONFLICT, "unmapped_ledger_status")

    targets = {
        LEDGER_STATUS_TO_RUNG_TERMINAL[e.status.strip().lower()] for e in terminal
    }
    if len(targets) > 1:
        return decide(RungVerdict.CONFLICT, "ledger_evidence_disagrees")

    target = next(iter(targets))
    if target == candidate.state:
        return decide(RungVerdict.NO_EVIDENCE, "already_in_target_state", target)

    try:
        sm.assert_rung_transition(candidate.state, target)
    except Exception:  # noqa: BLE001 - the state machine owns the message
        # e.g. `acked` -> `expired`: the broker says expired but the rung was
        # never recorded as resting. Real modelling conflict; a human decides.
        return decide(RungVerdict.CONFLICT, "transition_not_permitted", target)

    return decide(RungVerdict.TRANSITION, "terminal_broker_evidence", target)


def summarize(decisions: list[SweepDecision]) -> dict[str, object]:
    """Aggregate counts, always broken down by reason code and by side.

    No silent caps: every candidate lands in exactly one verdict bucket and the
    buy/sell split is always reported (the phantom population is mixed-side and
    a sell-only summary hides live buying-power impact).
    """
    by_verdict: dict[str, int] = {v.value: 0 for v in RungVerdict}
    by_reason: dict[str, int] = {}
    by_side: dict[str, dict[str, int]] = {}
    by_account: dict[str, dict[str, int]] = {}
    ownership_conflicts = 0
    for d in decisions:
        by_verdict[str(d.verdict)] += 1
        if d.ownership_conflict is not None:
            ownership_conflicts += 1
        by_reason[d.reason_code] = by_reason.get(d.reason_code, 0) + 1
        side_bucket = by_side.setdefault(d.candidate.side, dict.fromkeys(by_verdict, 0))
        side_bucket[str(d.verdict)] += 1
        acct_bucket = by_account.setdefault(
            d.candidate.account_mode, dict.fromkeys(by_verdict, 0)
        )
        acct_bucket[str(d.verdict)] += 1
    return {
        "candidates": len(decisions),
        "by_verdict": by_verdict,
        "by_reason_code": dict(sorted(by_reason.items())),
        "by_side": by_side,
        "by_account_mode": by_account,
        # Rungs where at least one matched ledger row was refused attribution
        # and dropped. Counted at the top level so it cannot be lost in the
        # per-reason breakdown of whatever the surviving subset classified as.
        "ownership_conflicts": ownership_conflicts,
    }
