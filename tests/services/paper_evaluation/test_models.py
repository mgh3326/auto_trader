from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

import app.models as model_exports
from app.models.paper_evaluation import (
    EvaluationConfig,
    EvaluationEpoch,
    EvaluationScorecard,
    EvaluationVerdict,
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


def _index_names(model: type) -> set[str]:
    return {
        index.name
        for index in model.__table__.indexes
        if index.name is not None
    }


def test_models_are_exposed_via_package_exports() -> None:
    for cls in (
        EvaluationConfig,
        EvaluationEpoch,
        EvaluationScorecard,
        EvaluationVerdict,
    ):
        assert hasattr(model_exports, cls.__name__)


def test_models_use_research_schema_and_exact_table_names() -> None:
    assert {
        model.__table__.name: model.__table__.schema
        for model in (
            EvaluationConfig,
            EvaluationEpoch,
            EvaluationScorecard,
            EvaluationVerdict,
        )
    } == {
        "evaluation_configs": "research",
        "evaluation_epochs": "research",
        "evaluation_scorecards": "research",
        "evaluation_verdicts": "research",
    }


def test_no_model_has_updated_at_column() -> None:
    for model in (
        EvaluationConfig,
        EvaluationEpoch,
        EvaluationScorecard,
        EvaluationVerdict,
    ):
        assert "updated_at" not in model.__table__.columns


# ---------------------------------------------------------------------------
# EvaluationConfig
# ---------------------------------------------------------------------------


def test_evaluation_config_columns() -> None:
    columns = EvaluationConfig.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "config_hash",
        "schema_id",
        "formula_version",
        "currency_conversion_policy",
        "payload",
        "created_at",
    }
    assert isinstance(columns["id"].type, BigInteger)
    assert isinstance(columns["config_hash"].type, String)
    assert isinstance(columns["payload"].type, JSONB)
    assert isinstance(columns["created_at"].type, DateTime)


def test_evaluation_config_constraints() -> None:
    assert {
        "uq_evaluation_config_hash",
        "ck_evaluation_config_hash",
        "ck_evaluation_config_schema_id",
        "ck_evaluation_config_formula_version",
        "ck_evaluation_config_currency_conversion_policy",
    } <= _constraint_names(EvaluationConfig)


# ---------------------------------------------------------------------------
# EvaluationEpoch
# ---------------------------------------------------------------------------


def test_evaluation_epoch_columns() -> None:
    columns = EvaluationEpoch.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "epoch_id",
        "cohort_id",
        "config_hash",
        "initial_equity",
        "started_at",
        "reset_reason",
        "prior_epoch_id",
        "experiment_hash",
        "cohort_hash",
        "created_at",
    }
    assert isinstance(columns["id"].type, BigInteger)
    assert isinstance(columns["initial_equity"].type, JSONB)
    assert isinstance(columns["started_at"].type, DateTime)
    assert columns["reset_reason"].nullable is True
    assert columns["prior_epoch_id"].nullable is True


def test_evaluation_epoch_constraints_and_foreign_keys() -> None:
    assert {
        "uq_evaluation_epoch_id",
        "uq_evaluation_epoch_lineage",
        "uq_evaluation_epoch_start",
        "ck_evaluation_epoch_config_hash",
        "ck_evaluation_epoch_experiment_hash",
        "ck_evaluation_epoch_cohort_hash",
        "ck_evaluation_epoch_reset_reason",
    } <= _constraint_names(EvaluationEpoch)
    assert {
        "fk_evaluation_epoch_cohort",
        "fk_evaluation_epoch_config",
    } <= _foreign_key_names(EvaluationEpoch)
    assert "ix_evaluation_epoch_cohort_started" in _index_names(EvaluationEpoch)


# ---------------------------------------------------------------------------
# EvaluationScorecard
# ---------------------------------------------------------------------------


def test_evaluation_scorecard_columns() -> None:
    columns = EvaluationScorecard.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "epoch_id",
        "config_hash",
        "view_name",
        "currency",
        "experiment_hash",
        "cohort_hash",
        "metrics",
        "created_at",
    }
    assert isinstance(columns["id"].type, BigInteger)
    assert isinstance(columns["metrics"].type, JSONB)
    assert isinstance(columns["view_name"].type, String)
    assert isinstance(columns["currency"].type, String)


def test_evaluation_scorecard_constraints_and_foreign_keys() -> None:
    assert {
        "uq_evaluation_scorecard_epoch_view",
        "ck_evaluation_scorecard_config_hash",
        "ck_evaluation_scorecard_experiment_hash",
        "ck_evaluation_scorecard_cohort_hash",
        "ck_evaluation_scorecard_view_name",
        "ck_evaluation_scorecard_currency",
        "ck_evaluation_scorecard_view_currency_consistency",
    } <= _constraint_names(EvaluationScorecard)
    assert "fk_evaluation_scorecard_epoch" in _foreign_key_names(
        EvaluationScorecard
    )
    assert "ix_evaluation_scorecard_epoch" in _index_names(EvaluationScorecard)


# ---------------------------------------------------------------------------
# EvaluationVerdict
# ---------------------------------------------------------------------------


def test_evaluation_verdict_columns() -> None:
    columns = EvaluationVerdict.__table__.columns
    assert set(columns.keys()) == {
        "id",
        "epoch_id",
        "config_hash",
        "idempotency_key",
        "request_hash",
        "verdict_status",
        "verdict_payload",
        "experiment_hash",
        "cohort_hash",
        "created_at",
    }
    assert isinstance(columns["id"].type, BigInteger)
    assert isinstance(columns["verdict_payload"].type, JSONB)
    assert isinstance(columns["verdict_status"].type, String)


def test_evaluation_verdict_constraints_and_foreign_keys() -> None:
    assert {
        "uq_evaluation_verdict_epoch",
        "uq_evaluation_verdict_idempotency",
        "ck_evaluation_verdict_config_hash",
        "ck_evaluation_verdict_request_hash",
        "ck_evaluation_verdict_experiment_hash",
        "ck_evaluation_verdict_cohort_hash",
        "ck_evaluation_verdict_status",
    } <= _constraint_names(EvaluationVerdict)
    assert "fk_evaluation_verdict_epoch" in _foreign_key_names(
        EvaluationVerdict
    )
    assert "ix_evaluation_verdict_epoch" in _index_names(EvaluationVerdict)
