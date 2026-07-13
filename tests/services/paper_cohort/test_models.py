from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
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
        )
    } == {
        "paper_validation_cohorts": "research",
        "paper_validation_cohort_assignments": "research",
        "canonical_market_snapshots": "research",
        "paper_cohort_decisions": "research",
        "paper_cohort_venue_intents": "research",
        "paper_cohort_run_claims": "research",
        "paper_run_order_links": "research",
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
        "decision_id",
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
    ):
        assert "updated_at" not in immutable_model.__table__.columns
