"""ROB-1286 §101차 ⑤ — the claim lifecycle, and what "done" means.

One fire, one outcome
---------------------
A claim starts at :data:`ClaimLifecycle.STARTED` and must reach exactly one
of three terminals:

``PROPOSAL_CREATED``
    The session called ``order_proposal_create`` and a proposal row exists.
    The proposal id is carried on the terminal so the event -> proposal
    mapping is a stored fact, not a narration.
``REJECTED_WITH_REASON``
    The session decided not to propose, and said why **for this event**.
    The reason is required and non-blank; there is no way to reach this
    terminal without one.
``EXPIRED_UNPROCESSED``
    The lease ran out with no terminal. Only the TTL path may write it.
    The flow then re-spawns at ``generation + 1``, and fencing stops the
    stale owner from writing over the new one.
``AWAITING_RECONCILE``
    r2 / BLOCKER 2. The spawn was ambiguous and could not be reconciled,
    so whether ``order_proposal_create`` committed is **unknown**. r1
    handled this by logging "will NOT be retried automatically" and
    leaving the claim ``STARTED`` -- but a ``STARTED`` claim is exactly
    what the lease expires, so thirty minutes later the TTL wrote
    ``EXPIRED_UNPROCESSED``, the flow re-claimed at ``generation + 1``,
    and the fire was judged again. If the first call had committed and
    only its acknowledgement was lost, that is **two proposals from one
    fire**, in front of the auto-approve lane.

    So "no blind retry" is a state, not a log line. This is a terminal;
    the TTL sweep only touches ``STARTED`` and therefore cannot walk it
    back, and both claim stores refuse to re-claim an event that holds it.
    It goes to an operator instead.

Terminal is not the same as resolved
------------------------------------
Four terminals, but only **two** of them are outcomes.
:data:`RESOLVED_LIFECYCLE_STATES` is the pair that satisfies the
completion criterion; ``EXPIRED_UNPROCESSED`` and ``AWAITING_RECONCILE``
are terminal *faults*, and counting either as done would relabel the
original accident as success. Keeping the two sets separate is what stops
"we stopped touching it" from drifting into "we finished it".

There is deliberately **no analysis-only terminal**
---------------------------------------------------
An operator decision (§101차): a session that writes a report and proposes
nothing, without an event-attributed reason, is a failure -- not a third
kind of success. So no ``analysed`` / ``reviewed`` / ``no_action`` member
exists in this enum, and no code path can reach one. A session that only
analysed has either produced a rejection reason (``REJECTED_WITH_REASON``)
or has not finished (``STARTED``, and later ``EXPIRED_UNPROCESSED``).

This is the enum-level half of the completion criterion. The mapping half
-- every polled event resolving to a proposal id or a rejection reason --
is :func:`build_completion_mapping`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "NON_RECLAIMABLE_STATES",
    "RESOLVED_LIFECYCLE_STATES",
    "TERMINAL_LIFECYCLE_STATES",
    "ClaimLifecycle",
    "CompletionRow",
    "IncompleteOutcome",
    "SessionOutcome",
    "awaiting_reconcile",
    "build_completion_mapping",
    "rejected",
    "proposal_created",
]


class ClaimLifecycle(StrEnum):
    """The only states a repricing claim may hold."""

    STARTED = "started"
    PROPOSAL_CREATED = "proposal_created"
    REJECTED_WITH_REASON = "rejected_with_reason"
    EXPIRED_UNPROCESSED = "expired_unprocessed"
    AWAITING_RECONCILE = "awaiting_reconcile"


# The only two states that answer the completion criterion. Everything
# else is either in flight or a fault.
RESOLVED_LIFECYCLE_STATES = frozenset(
    {
        ClaimLifecycle.PROPOSAL_CREATED,
        ClaimLifecycle.REJECTED_WITH_REASON,
    }
)

# States a claim may end in. A terminal is never re-claimed except
# ``EXPIRED_UNPROCESSED``, which exists precisely so a crashed tick's fire
# is picked up again.
TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        *RESOLVED_LIFECYCLE_STATES,
        ClaimLifecycle.EXPIRED_UNPROCESSED,
        ClaimLifecycle.AWAITING_RECONCILE,
    }
)

# Terminals that must survive the TTL and block a re-claim. r2 / BLOCKER 2:
# the whole point of ``AWAITING_RECONCILE`` is that no clock walks it back.
NON_RECLAIMABLE_STATES = frozenset(
    {
        *RESOLVED_LIFECYCLE_STATES,
        ClaimLifecycle.AWAITING_RECONCILE,
    }
)


class IncompleteOutcome(ValueError):
    """A session outcome that would resolve a fire to nothing."""


@dataclass(frozen=True)
class SessionOutcome:
    """What a re-judgement session produced for one fire.

    Constructed only through :func:`proposal_created` or :func:`rejected`,
    both of which refuse an empty payload. A dataclass alone would let a
    caller build ``SessionOutcome(PROPOSAL_CREATED, proposal_id=None)`` --
    the exact "reported success, produced nothing" shape the completion
    criterion exists to catch -- so the invariant is checked here too.
    """

    state: ClaimLifecycle
    proposal_id: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.state is ClaimLifecycle.PROPOSAL_CREATED:
            if not (self.proposal_id or "").strip():
                raise IncompleteOutcome(
                    "PROPOSAL_CREATED requires a non-blank proposal_id"
                )
            if self.rejection_reason is not None:
                raise IncompleteOutcome(
                    "PROPOSAL_CREATED must not also carry a rejection_reason"
                )
        elif self.state is ClaimLifecycle.REJECTED_WITH_REASON:
            if not (self.rejection_reason or "").strip():
                raise IncompleteOutcome(
                    "REJECTED_WITH_REASON requires a non-blank, event-attributed "
                    "reason; a session that analysed and said nothing is a "
                    "failure, not a third outcome"
                )
            if self.proposal_id is not None:
                raise IncompleteOutcome(
                    "REJECTED_WITH_REASON must not also carry a proposal_id"
                )
        elif self.state is ClaimLifecycle.EXPIRED_UNPROCESSED:
            if self.proposal_id or self.rejection_reason:
                raise IncompleteOutcome(
                    "EXPIRED_UNPROCESSED is the TTL terminal and carries neither "
                    "a proposal nor a reason"
                )
        elif self.state is ClaimLifecycle.AWAITING_RECONCILE:
            if self.proposal_id or self.rejection_reason:
                raise IncompleteOutcome(
                    "AWAITING_RECONCILE means nobody knows what the session did; "
                    "carrying a proposal id or a reason would claim knowledge "
                    "this terminal exists to say we lack"
                )
        else:
            raise IncompleteOutcome(
                f"{self.state} is not a terminal outcome; a session may not "
                "finish in STARTED"
            )


def proposal_created(proposal_id: str) -> SessionOutcome:
    return SessionOutcome(
        state=ClaimLifecycle.PROPOSAL_CREATED, proposal_id=proposal_id
    )


def rejected(reason: str) -> SessionOutcome:
    return SessionOutcome(
        state=ClaimLifecycle.REJECTED_WITH_REASON, rejection_reason=reason
    )


def awaiting_reconcile() -> SessionOutcome:
    """The terminal for an ambiguous spawn. Carries no claim of knowledge.

    Written by the tick, not by a session -- the session is exactly what we
    could not get an answer from.
    """
    return SessionOutcome(state=ClaimLifecycle.AWAITING_RECONCILE)


@dataclass(frozen=True)
class CompletionRow:
    """One row of the event -> outcome mapping table."""

    event_uuid: str
    symbol: str
    state: str
    proposal_id: str | None
    rejection_reason: str | None
    deferral_reason: str | None = None

    @property
    def is_quarantined(self) -> bool:
        """The tick does not know what happened, and a human must look.

        Kept distinct from :attr:`is_deferred` because the two make
        opposite promises. A deferred fire will be judged by a later tick.
        A quarantined one will not be -- it is terminal by design (r2 /
        BLOCKER 2), because the alternative is re-judging a fire that may
        already have produced a proposal. Reporting it as "deferred" would
        say a retry is coming when none is.
        """
        return self.state == ClaimLifecycle.AWAITING_RECONCILE

    @property
    def is_deferred(self) -> bool:
        """The tick knowingly did not judge this fire *yet*.

        Deferral is a real, bounded thing -- two rungs on one symbol cannot
        both hold the single per-symbol slot, and the round cap exists to
        bound blast radius -- so pretending it does not happen would be a
        lie. But it is emphatically **not** an outcome: a deferred fire has
        produced neither a proposal nor a judgement, and a run that ends
        with one is not complete. It only stops being a *failure* when a
        later tick resolves it, which is what
        :attr:`CompletionReport.is_complete` refuses to assume.
        """
        if self.is_quarantined:
            return False
        return bool((self.deferral_reason or "").strip()) and not self.is_resolved

    @property
    def is_resolved(self) -> bool:
        """True iff this fire produced a proposal or an attributed reason.

        ``EXPIRED_UNPROCESSED`` is deliberately **not** resolved: the TTL
        terminal means nobody judged the fire, and counting it as done
        would relabel the original accident as success.
        """
        if self.state == ClaimLifecycle.PROPOSAL_CREATED:
            return bool((self.proposal_id or "").strip())
        if self.state == ClaimLifecycle.REJECTED_WITH_REASON:
            return bool((self.rejection_reason or "").strip())
        # EXPIRED_UNPROCESSED and AWAITING_RECONCILE are terminal faults.
        return False


@dataclass(frozen=True)
class CompletionReport:
    """The 1:1 mapping the completion criterion is judged on."""

    rows: tuple[CompletionRow, ...]

    @property
    def unresolved(self) -> tuple[CompletionRow, ...]:
        return tuple(row for row in self.rows if not row.is_resolved)

    @property
    def deferred(self) -> tuple[CompletionRow, ...]:
        return tuple(row for row in self.rows if row.is_deferred)

    @property
    def quarantined(self) -> tuple[CompletionRow, ...]:
        """Fires whose outcome is unknown and which need an operator."""
        return tuple(row for row in self.rows if row.is_quarantined)

    @property
    def unaccounted(self) -> tuple[CompletionRow, ...]:
        """Fires with neither an outcome nor even a stated reason for waiting.

        These are the ones that vanished, which is the accident.
        """
        return tuple(
            row
            for row in self.rows
            if not row.is_resolved and not row.is_deferred and not row.is_quarantined
        )

    @property
    def is_accounted(self) -> bool:
        """Per-tick floor: nothing disappeared without a word."""
        return bool(self.rows) and not self.unaccounted

    @property
    def is_complete(self) -> bool:
        """Run-level bar: every fire produced a proposal or a reason.

        Deliberately **not** satisfied by deferral. A tick that deferred a
        fire has not judged it, and the loop must be shown to converge --
        by a later tick actually resolving it -- rather than credited for
        having a reason to wait.
        """
        return bool(self.rows) and not self.unresolved

    def as_table(self) -> list[dict[str, str | None]]:
        return [
            {
                "eventUuid": row.event_uuid,
                "symbol": row.symbol,
                "state": row.state,
                "proposalId": row.proposal_id,
                "rejectionReason": row.rejection_reason,
                "deferralReason": row.deferral_reason,
            }
            for row in self.rows
        ]


def build_completion_mapping(
    *,
    polled_event_uuids: list[tuple[str, str]],
    outcomes: dict[str, SessionOutcome],
    deferrals: dict[str, str] | None = None,
    quarantined: dict[str, str] | None = None,
) -> CompletionReport:
    """Map every polled fire to its outcome, or to nothing.

    ``polled_event_uuids`` is the set N -- fixed *before* the run, from what
    the poll actually saw -- as ``(event_uuid, symbol)`` pairs. An event with
    no outcome becomes an unresolved row rather than being dropped, because a
    mapping that silently omits its failures cannot be used to judge
    completeness.
    """
    deferrals = deferrals or {}
    quarantined = quarantined or {}
    rows: list[CompletionRow] = []
    for event_uuid, symbol in polled_event_uuids:
        outcome = outcomes.get(event_uuid)
        if outcome is None and event_uuid in quarantined:
            # Terminal, unresolved, and not coming back on a later tick.
            rows.append(
                CompletionRow(
                    event_uuid=event_uuid,
                    symbol=symbol,
                    state=str(ClaimLifecycle.AWAITING_RECONCILE),
                    proposal_id=None,
                    rejection_reason=None,
                    deferral_reason=quarantined[event_uuid],
                )
            )
            continue
        if outcome is None:
            deferral = deferrals.get(event_uuid)
            rows.append(
                CompletionRow(
                    event_uuid=event_uuid,
                    symbol=symbol,
                    state="deferred" if deferral else "unmapped",
                    proposal_id=None,
                    rejection_reason=None,
                    deferral_reason=deferral,
                )
            )
            continue
        rows.append(
            CompletionRow(
                event_uuid=event_uuid,
                symbol=symbol,
                state=str(outcome.state),
                proposal_id=outcome.proposal_id,
                rejection_reason=outcome.rejection_reason,
            )
        )
    return CompletionReport(rows=tuple(rows))
