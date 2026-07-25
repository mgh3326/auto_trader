from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.paper_validation import (
    PaperValidationPostmortemReview,
    PaperValidationStateTransition,
    StrategyHypothesisDraft,
)


def _constraint_names(model: type) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if constraint.name is not None
    }


def test_transition_model_is_append_only_event_shape_without_current_state() -> None:
    table = PaperValidationStateTransition.__table__

    assert table.schema == "research"
    assert table.name == "paper_validation_state_transitions"
    assert "current_state" not in table.columns
    assert {
        "validation_id",
        "validation_version",
        "experiment_id",
        "strategy_version_id",
        "cohort_id",
        "sequence",
        "idempotency_key",
        "request_hash",
        "prior_state",
        "new_state",
        "actor_id",
        "actor_role",
        "reason_code",
        "reason_text",
        "experiment_hash",
        "cohort_hash",
        "strategy_hash",
        "config_hash",
        "policy_hash",
        "input_hash",
        "evidence_ids",
        "created_at",
    } <= set(table.columns.keys())
    assert _constraint_names(PaperValidationStateTransition) >= {
        "uq_paper_validation_transition_sequence",
        "uq_paper_validation_transition_idempotency",
        "ck_paper_validation_transition_graph",
        "ck_paper_validation_transition_actor_role",
        "ck_paper_validation_transition_hashes",
    }
    assert sum(isinstance(item, UniqueConstraint) for item in table.constraints) >= 2
    assert sum(isinstance(item, CheckConstraint) for item in table.constraints) >= 3


def test_hypothesis_model_fixes_complete_schema_and_author_identity() -> None:
    table = StrategyHypothesisDraft.__table__

    assert table.schema == "research"
    assert {
        "mechanism",
        "universe",
        "horizon",
        "entry_criteria",
        "exit_criteria",
        "invalidation_criteria",
        "data_requirements",
        "expected_cost_hurdle",
        "turnover_bound",
        "risk_bound",
        "cited_evidence",
        "author_id",
        "author_role",
    } <= set(table.columns.keys())
    assert table.columns.author_role.nullable is False


def test_postmortem_model_keeps_narrative_separate_from_metrics_and_gates() -> None:
    table = PaperValidationPostmortemReview.__table__

    assert table.schema == "research"
    assert {"review_text", "cited_evidence", "evaluator_id", "evaluator_role"} <= (
        set(table.columns.keys())
    )
    assert "metrics" not in table.columns
    assert "gate_results" not in table.columns
    assert "active_strategy_payload" not in table.columns
