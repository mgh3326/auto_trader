"""ROB-816 order_proposals exception hierarchy.

All writes go through OrderProposalsService; these are the domain errors it raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.order_proposals.void_authorization import (
        VoidAuthorizationDecision,
    )


class OrderProposalError(Exception):
    """Base for all order_proposals domain errors."""


class OrderProposalInvalidStateTransition(OrderProposalError):
    """Raised when a rung/group state change violates the locked transition graph.

    The transition graph is defined in state_machine.py and duplicated in this
    docstring for locality:

        pending_approval -> {revalidating, rejected, voided, voided_local_stale, superseded}
        revalidating     -> {approved, needs_reconfirm, pending_approval, superseded, voided}
        needs_reconfirm  -> {pending_approval, rejected, superseded, voided}
        approved         -> {submitting, superseded, voided}
        submitting       -> {acked, resting, rejected, unverified}
        acked            -> {filled, partially_filled, cancelled, unverified}
        resting          -> {filled, partially_filled, cancelled, expired, unverified}
        partially_filled -> {filled, cancelled, expired, unverified}
        unverified       -> {filled, partially_filled, cancelled, expired, rejected,
                             voided_local_stale}
        draft            -> {pending_approval, voided}
        (terminal: filled, cancelled, expired, rejected, voided,
                   voided_local_stale, superseded)
    """


class OrderProposalNotFound(OrderProposalError):
    """No order_proposals row for the given proposal_id."""


class OrderProposalDuplicate(OrderProposalError):
    """A proposal with the same proposal_id already exists."""


class OrderProposalVoidNotAuthorized(OrderProposalError):
    """The requester may not retire this proposal (ROB-1238).

    Carries the pure ``VoidAuthorizationDecision`` so the MCP surface can return
    a structured ``reason_code``/``detail`` instead of a bare string. Raised
    whenever the requester neither created the proposal nor can point at a
    server-confirmed expiry or loss-guard violation -- i.e. every attempt to
    void another lane's live proposal.
    """

    def __init__(self, decision: VoidAuthorizationDecision) -> None:
        super().__init__(decision.reason_code)
        self.decision = decision


class OrderProposalDispatchNoLongerAuthorized(OrderProposalError):
    """The proposal stopped being dispatchable between recheck and submit.

    Raised by the atomic pre-submit gate (ROB-1238) when a re-read under the
    proposal row lock shows the proposal was voided, expired, superseded, or
    marked ``no_resubmit`` after the approval token was minted. The approval
    token is invalidated on this path so a stale Telegram button cannot retry.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OrderProposalUnsupportedTargetAction(OrderProposalError):
    """The requested account_mode/market/action combination is unsupported.

    Carries a structured ``supported_matrix`` (per-action allowed
    account_mode x market pairs, derived from the same capability sets the
    message text is generated from -- see ROB-972) and the rejected
    ``requested`` combination, so callers can render an accurate,
    action-specific rejection instead of a place-only message reused for
    replace/cancel.
    """

    def __init__(
        self,
        message: str,
        *,
        supported_matrix: dict[str, list[dict[str, str]]],
        requested: dict[str, str],
    ) -> None:
        super().__init__(message)
        self.supported_matrix = supported_matrix
        self.requested = requested
