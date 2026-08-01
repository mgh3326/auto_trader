"""Declarative mapping of current broker response meanings to the B-1 algebra.

The catalog documents existing semantics; it is not an executable response
adapter and no current caller imports it.  Source locators identify the legacy
implementation that each contract fixture freezes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.services.execution_outcomes.contract import (
    BrokerAcceptance,
    MutationOutcome,
    TerminalEvidence,
    TrackingState,
)


class BrokerSurface(StrEnum):
    GENERIC = "generic"
    KIS = "kis"
    KIWOOM = "kiwoom"
    TOSS = "toss"
    ALPACA = "alpaca"
    BINANCE = "binance"


class LegacySuccessMeaning(StrEnum):
    """Meaning of a legacy ``success`` boolean, if the response has one."""

    NO_SUCCESS_FIELD = "no_success_field"
    REQUEST_HANDLED = "request_handled"
    BROKER_ACCEPTED = "broker_accepted"
    BROKER_ACCEPTED_AND_LOCAL_RECORDED = "broker_accepted_and_local_recorded"
    FAILURE_WITH_DEFINITE_REJECTION = "failure_with_definite_rejection"
    FAILURE_WITH_UNKNOWN_ACCEPTANCE = "failure_with_unknown_acceptance"


@dataclass(frozen=True, slots=True)
class LegacyResponseMapping:
    mapping_id: str
    surface: BrokerSurface
    source_locator: str
    legacy_response_markers: tuple[str, ...]
    legacy_success_meaning: LegacySuccessMeaning
    outcome: MutationOutcome
    note: str

    def __post_init__(self) -> None:
        for field_name in ("mapping_id", "source_locator", "note"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not self.legacy_response_markers:
            raise ValueError("legacy_response_markers must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "mapping_id": self.mapping_id,
            "surface": self.surface.value,
            "source_locator": self.source_locator,
            "legacy_response_markers": list(self.legacy_response_markers),
            "legacy_success_meaning": self.legacy_success_meaning.value,
            "outcome": self.outcome.to_dict(),
            "note": self.note,
        }


def _outcome(
    *,
    request_validated: bool = True,
    mutation_sent: bool,
    acceptance: BrokerAcceptance,
    tracking: TrackingState,
    local_recorded: bool,
    reconcile_required: bool,
    evidence: TerminalEvidence = TerminalEvidence.NONE,
) -> MutationOutcome:
    return MutationOutcome(
        request_validated=request_validated,
        mutation_sent=mutation_sent,
        broker_acceptance=acceptance,
        tracking=tracking,
        local_recorded=local_recorded,
        reconcile_required=reconcile_required,
        terminal_evidence=evidence,
    )


CURRENT_RESPONSE_MAPPINGS = (
    LegacyResponseMapping(
        mapping_id="generic.dry_run_success",
        surface=BrokerSurface.GENERIC,
        source_locator="app/mcp_server/tooling/order_execution.py::_place_order_impl",
        legacy_response_markers=("success=true", "dry_run=true"),
        legacy_success_meaning=LegacySuccessMeaning.REQUEST_HANDLED,
        outcome=_outcome(
            mutation_sent=False,
            acceptance=BrokerAcceptance.NOT_SENT,
            tracking=TrackingState.NOT_APPLICABLE,
            local_recorded=False,
            reconcile_required=False,
        ),
        note="A successful preview validates a request but sends no mutation.",
    ),
    LegacyResponseMapping(
        mapping_id="generic.send_outcome_unknown",
        surface=BrokerSurface.GENERIC,
        source_locator=(
            "app/mcp_server/tooling/order_execution.py::"
            "_augment_error_for_unknown_outcome"
        ),
        legacy_response_markers=(
            "success=false",
            "outcome_unknown=true",
            "reconcile_tool=market_specific",
        ),
        legacy_success_meaning=LegacySuccessMeaning.FAILURE_WITH_UNKNOWN_ACCEPTANCE,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.UNKNOWN,
            tracking=TrackingState.UNKNOWN,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="A send-time timeout may have created an order; never auto-resend.",
    ),
    LegacyResponseMapping(
        mapping_id="generic.live_accepted_pending_fill",
        surface=BrokerSurface.GENERIC,
        source_locator="app/mcp_server/tooling/live_order_ledger.py::_record_live_order",
        legacy_response_markers=(
            "success=true",
            "broker_status=accepted",
            "fill_recorded=false",
        ),
        legacy_success_meaning=(
            LegacySuccessMeaning.BROKER_ACCEPTED_AND_LOCAL_RECORDED
        ),
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=True,
            reconcile_required=True,
        ),
        note="Accepted-only ledger recording is not fill evidence.",
    ),
    LegacyResponseMapping(
        mapping_id="kis.live_accepted_pending_fill",
        surface=BrokerSurface.KIS,
        source_locator="app/mcp_server/tooling/kis_live_ledger.py::_record_kis_live_order",
        legacy_response_markers=(
            "success=true",
            "broker_status=accepted",
            "fill_recorded=false",
            "order_id=present",
        ),
        legacy_success_meaning=(
            LegacySuccessMeaning.BROKER_ACCEPTED_AND_LOCAL_RECORDED
        ),
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=True,
            reconcile_required=True,
        ),
        note="KIS send records accepted-only; reconcile owns fill truth.",
    ),
    LegacyResponseMapping(
        mapping_id="kiwoom.submitted_tracked",
        surface=BrokerSurface.KIWOOM,
        source_locator=(
            "app/mcp_server/tooling/orders_kiwoom_shared.py::"
            "finalize_place_broker_response"
        ),
        legacy_response_markers=(
            "success=true",
            "status=submitted",
            "reconcile_required=false",
            "order_id=present",
        ),
        legacy_success_meaning=LegacySuccessMeaning.BROKER_ACCEPTED,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=False,
        ),
        note="Submitted is tracked broker acceptance, not terminal execution.",
    ),
    LegacyResponseMapping(
        mapping_id="kiwoom.accepted_untracked",
        surface=BrokerSurface.KIWOOM,
        source_locator=(
            "app/mcp_server/tooling/orders_kiwoom_shared.py::"
            "finalize_place_broker_response"
        ),
        legacy_response_markers=(
            "success=false",
            "status=accepted_untracked",
            "reconcile_required=true",
            "retry_allowed=false",
        ),
        legacy_success_meaning=LegacySuccessMeaning.BROKER_ACCEPTED,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.UNTRACKED,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="Broker acceptance without one valid order id remains untracked.",
    ),
    LegacyResponseMapping(
        mapping_id="kiwoom.acceptance_uncertain",
        surface=BrokerSurface.KIWOOM,
        source_locator=(
            "app/mcp_server/tooling/orders_kiwoom_variants.py::"
            "_dispatch_unknown_response"
        ),
        legacy_response_markers=(
            "success=false",
            "status=acceptance_uncertain",
            "reconcile_required=true",
            "retry_allowed=false",
        ),
        legacy_success_meaning=LegacySuccessMeaning.FAILURE_WITH_UNKNOWN_ACCEPTANCE,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.UNKNOWN,
            tracking=TrackingState.UNKNOWN,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="Post-dispatch ambiguity cannot be converted into a safe retry.",
    ),
    LegacyResponseMapping(
        mapping_id="toss.accepted_recorded",
        surface=BrokerSurface.TOSS,
        source_locator="app/mcp_server/tooling/orders_toss_variants.py::execute_order",
        legacy_response_markers=(
            "success=true",
            "mutation_sent=true",
            "order_id=present",
            "message=accepted_and_recorded_accepted_only",
        ),
        legacy_success_meaning=(
            LegacySuccessMeaning.BROKER_ACCEPTED_AND_LOCAL_RECORDED
        ),
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=True,
            reconcile_required=True,
        ),
        note="Toss success is accepted-only plus local ledger recording.",
    ),
    LegacyResponseMapping(
        mapping_id="toss.http_rejected",
        surface=BrokerSurface.TOSS,
        source_locator=(
            "app/mcp_server/tooling/orders_toss_variants.py::_toss_error_response"
        ),
        legacy_response_markers=(
            "success=false",
            "mutation_sent=true",
            "status_code=4xx",
            "code=broker_error_code",
        ),
        legacy_success_meaning=LegacySuccessMeaning.FAILURE_WITH_DEFINITE_REJECTION,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.REJECTED,
            tracking=TrackingState.NOT_APPLICABLE,
            local_recorded=False,
            reconcile_required=False,
        ),
        note="A typed HTTP 4xx envelope is definitive broker rejection.",
    ),
    LegacyResponseMapping(
        mapping_id="toss.transport_unknown",
        surface=BrokerSurface.TOSS,
        source_locator=(
            "app/mcp_server/tooling/orders_toss_variants.py::_toss_error_response"
        ),
        legacy_response_markers=(
            "success=false",
            "mutation_sent=true",
            "status_code=absent",
            "error=transport_exception",
        ),
        legacy_success_meaning=LegacySuccessMeaning.FAILURE_WITH_UNKNOWN_ACCEPTANCE,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.UNKNOWN,
            tracking=TrackingState.UNKNOWN,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="A transport exception after dispatch is ambiguous, not rejected.",
    ),
    LegacyResponseMapping(
        mapping_id="alpaca.confirmation_preview",
        surface=BrokerSurface.ALPACA,
        source_locator=(
            "app/mcp_server/tooling/alpaca_paper_orders.py::alpaca_paper_submit_order"
        ),
        legacy_response_markers=(
            "success=true",
            "submitted=false",
            "blocked_reason=confirmation_required",
        ),
        legacy_success_meaning=LegacySuccessMeaning.REQUEST_HANDLED,
        outcome=_outcome(
            mutation_sent=False,
            acceptance=BrokerAcceptance.NOT_SENT,
            tracking=TrackingState.NOT_APPLICABLE,
            local_recorded=False,
            reconcile_required=False,
        ),
        note="Handler success with confirm=false is not broker acceptance.",
    ),
    LegacyResponseMapping(
        mapping_id="alpaca.cancel_accepted_unconfirmed",
        surface=BrokerSurface.ALPACA,
        source_locator=(
            "app/mcp_server/tooling/alpaca_paper_orders.py::alpaca_paper_cancel_order"
        ),
        legacy_response_markers=(
            "success=true",
            "cancel_requested=true",
            "cancelled=false",
        ),
        legacy_success_meaning=LegacySuccessMeaning.BROKER_ACCEPTED,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="DELETE acceptance is not terminal cancelled evidence.",
    ),
    LegacyResponseMapping(
        mapping_id="alpaca.cancel_confirmed",
        surface=BrokerSurface.ALPACA,
        source_locator=(
            "app/mcp_server/tooling/alpaca_paper_orders.py::alpaca_paper_cancel_order"
        ),
        legacy_response_markers=(
            "success=true",
            "cancel_requested=true",
            "cancelled=true",
            "lifecycle_synced=true",
        ),
        legacy_success_meaning=(
            LegacySuccessMeaning.BROKER_ACCEPTED_AND_LOCAL_RECORDED
        ),
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=True,
            reconcile_required=False,
            evidence=TerminalEvidence.CANCELLED,
        ),
        note="Only broker read-back status=canceled proves terminal cancellation.",
    ),
    LegacyResponseMapping(
        mapping_id="binance.submit_new",
        surface=BrokerSurface.BINANCE,
        source_locator=(
            "app/services/brokers/binance/*_demo/execution_client.py::submit_order"
        ),
        legacy_response_markers=(
            "result=OrderSubmitResult",
            "broker_order_id=present",
            "status=NEW",
        ),
        legacy_success_meaning=LegacySuccessMeaning.NO_SUCCESS_FIELD,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=True,
        ),
        note="A NEW submit result is accepted/tracked but not filled.",
    ),
    LegacyResponseMapping(
        mapping_id="binance.submit_partial_fill",
        surface=BrokerSurface.BINANCE,
        source_locator=(
            "app/services/brokers/binance/*_demo/dto.py::OrderSubmitResult.status"
        ),
        legacy_response_markers=(
            "result=OrderSubmitResult",
            "status=PARTIALLY_FILLED",
            "executed_qty>0",
        ),
        legacy_success_meaning=LegacySuccessMeaning.NO_SUCCESS_FIELD,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=True,
            evidence=TerminalEvidence.PARTIAL_FILL,
        ),
        note="Partial fill is non-terminal; tracking/reconciliation must continue.",
    ),
    LegacyResponseMapping(
        mapping_id="binance.submit_filled_unrecorded",
        surface=BrokerSurface.BINANCE,
        source_locator=(
            "app/services/brokers/binance/*_demo/dto.py::OrderSubmitResult.status"
        ),
        legacy_response_markers=(
            "result=OrderSubmitResult",
            "status=FILLED",
            "local_ledger_write=not_part_of_dto",
        ),
        legacy_success_meaning=LegacySuccessMeaning.NO_SUCCESS_FIELD,
        outcome=_outcome(
            mutation_sent=True,
            acceptance=BrokerAcceptance.ACCEPTED,
            tracking=TrackingState.TRACKED,
            local_recorded=False,
            reconcile_required=True,
            evidence=TerminalEvidence.FILLED,
        ),
        note="FILLED is terminal broker evidence but still needs local recording.",
    ),
)


def _validate_catalog() -> None:
    ids = [entry.mapping_id for entry in CURRENT_RESPONSE_MAPPINGS]
    if len(ids) != len(set(ids)):
        raise RuntimeError("execution outcome mapping ids must be unique")
    surfaces = {entry.surface for entry in CURRENT_RESPONSE_MAPPINGS}
    if surfaces != set(BrokerSurface):
        raise RuntimeError("execution outcome catalog must cover every broker surface")


_validate_catalog()


def mapping_catalog_as_data() -> list[dict[str, object]]:
    """Return the catalog in deterministic fixture order."""

    return [entry.to_dict() for entry in CURRENT_RESPONSE_MAPPINGS]


__all__ = [
    "BrokerSurface",
    "CURRENT_RESPONSE_MAPPINGS",
    "LegacyResponseMapping",
    "LegacySuccessMeaning",
    "mapping_catalog_as_data",
]
