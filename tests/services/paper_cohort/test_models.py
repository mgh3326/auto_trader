from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

import app.models as model_exports
from app.models import paper_cohort as paper_cohort_models
from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortTargetReservation,
    PaperCohortTerminalFence,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)


def _constraint_names(model: type) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint | UniqueConstraint)
        and constraint.name is not None
    }


def _foreign_key_names(model: type) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name is not None
    }


def test_models_expose_full_lineage_reservation_fence_and_claim_state() -> None:
    assert hasattr(paper_cohort_models, "PaperCohortTargetReservation")
    assert hasattr(paper_cohort_models, "PaperCohortTerminalFence")
    assert hasattr(model_exports, "PaperCohortTargetReservation")
    assert hasattr(model_exports, "PaperCohortTerminalFence")
    reservation = paper_cohort_models.PaperCohortTargetReservation
    fence = paper_cohort_models.PaperCohortTerminalFence

    assert set(PaperCohortVenueIntent.__table__.columns.keys()) >= {
        "round_decision_id",
        "assignment_id",
        "symbol",
        "execution_ordinal",
    }
    assert set(PaperRunOrderLink.__table__.columns.keys()) >= {
        "round_decision_id",
        "intent_id",
        "assignment_id",
        "symbol",
    }
    assert set(PaperCohortRunClaim.__table__.columns.keys()) >= {
        "claim_status",
        "terminal_reason",
        "terminal_at",
    }
    assert str(PaperCohortRunClaim.__table__.c.claim_status.server_default.arg) == (
        "'in_progress'"
    )
    assert reservation.__table__.schema == fence.__table__.schema == "research"
    assert {
        "uq_paper_cohort_target_reservation_target",
    } <= _constraint_names(reservation)
    assert {
        "uq_paper_cohort_terminal_fence_id",
        "uq_paper_cohort_terminal_fence_cohort",
        "uq_paper_cohort_terminal_fence_idempotency",
    } <= _constraint_names(fence)
    assert {
        "fk_paper_cohort_decision_assignment_lineage",
        "fk_paper_cohort_decision_snapshot_lineage",
    } <= _foreign_key_names(PaperCohortDecision)
    assert "fk_paper_cohort_intent_decision_lineage" in _foreign_key_names(
        PaperCohortVenueIntent
    )
    assert "fk_paper_run_order_link_intent_lineage" in _foreign_key_names(
        PaperRunOrderLink
    )
    assert "fk_paper_cohort_target_reservation_intent_lineage" in _foreign_key_names(
        reservation
    )


def test_models_use_research_schema_and_exact_table_names() -> None:
    assert {
        model.__table__.name: model.__table__.schema
        for model in (
            PaperValidationCohort,
            PaperValidationCohortAssignment,
            CanonicalMarketSnapshot,
            PaperCohortDecision,
            PaperCohortVenueIntent,
            PaperCohortRunClaim,
            PaperRunOrderLink,
            PaperCohortTargetReservation,
            PaperCohortTerminalFence,
        )
    } == {
        "paper_validation_cohorts": "research",
        "paper_validation_cohort_assignments": "research",
        "canonical_market_snapshots": "research",
        "paper_cohort_decisions": "research",
        "paper_cohort_venue_intents": "research",
        "paper_cohort_run_claims": "research",
        "paper_run_order_links": "research",
        "paper_cohort_target_reservations": "research",
        "paper_cohort_terminal_fences": "research",
    }


def test_cohort_and_assignment_constraints_are_explicit() -> None:
    assert {
        "uq_paper_validation_cohort_id",
        "ck_paper_validation_cohort_venues",
        "ck_paper_validation_cohort_symbols",
        "ck_paper_validation_cohort_market",
        "ck_paper_validation_cohort_leverage",
        "ck_paper_validation_cohort_interval",
        "ck_paper_validation_cohort_capture_limits",
        "ck_paper_validation_cohort_capital",
    } <= _constraint_names(PaperValidationCohort)
    assert {
        "uq_paper_cohort_assignment_id",
        "uq_paper_cohort_assignment_ordinal",
        "uq_paper_cohort_assignment_experiment",
        "ck_paper_cohort_assignment_role_ordinal",
        "ck_paper_cohort_assignment_hashes",
        "ck_paper_cohort_assignment_weights",
    } <= _constraint_names(PaperValidationCohortAssignment)


def test_snapshot_decision_intent_and_claim_exactly_once_constraints() -> None:
    assert "uq_canonical_snapshot_round" in _constraint_names(CanonicalMarketSnapshot)
    assert "uq_paper_cohort_decision_identity" in _constraint_names(PaperCohortDecision)
    assert "uq_paper_cohort_venue_intent" in _constraint_names(PaperCohortVenueIntent)
    assert "uq_paper_cohort_run_claim" in _constraint_names(PaperCohortRunClaim)


def test_thin_link_schema_has_only_identity_columns() -> None:
    assert set(PaperRunOrderLink.__table__.columns.keys()) == {
        "id",
        "cohort_id",
        "run_id",
        "round_decision_id",
        "intent_id",
        "decision_id",
        "assignment_id",
        "symbol",
        "snapshot_id",
        "snapshot_hash",
        "venue",
        "native_ledger_kind",
        "native_ledger_row_id",
        "client_order_id",
        "broker_order_id",
        "created_at",
    }
    forbidden_fragments = {
        "fill",
        "status",
        "lifecycle",
        "executed",
        "price",
        "fee",
        "pnl",
        "profit",
        "loss",
    }
    assert not any(
        fragment in column.name.lower()
        for column in PaperRunOrderLink.__table__.columns
        for fragment in forbidden_fragments
    )
    assert {
        "uq_paper_run_order_link_intent",
        "uq_paper_run_order_link_native_row",
        "uq_paper_run_order_link_client_order",
    } <= _constraint_names(PaperRunOrderLink)


def test_only_run_claim_carries_mutable_orchestration_fields() -> None:
    claim_columns = set(PaperCohortRunClaim.__table__.columns.keys())
    assert {
        "request_hash",
        "owner_token",
        "lease_expires_at",
        "result_payload",
        "completed_at",
    } <= claim_columns
    for immutable_model in (
        PaperValidationCohort,
        PaperValidationCohortAssignment,
        CanonicalMarketSnapshot,
        PaperCohortDecision,
        PaperCohortVenueIntent,
        PaperRunOrderLink,
        PaperCohortTargetReservation,
        PaperCohortTerminalFence,
    ):
        assert "updated_at" not in immutable_model.__table__.columns
