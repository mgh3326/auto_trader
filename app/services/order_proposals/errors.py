"""ROB-816 order_proposals exception hierarchy.

All writes go through OrderProposalsService; these are the domain errors it raises.
"""

from __future__ import annotations


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
