from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.models.analysis_artifact import AnalysisArtifact


@pytest.mark.unit
def test_analysis_artifact_model_contract() -> None:
    assert AnalysisArtifact.__tablename__ == "analysis_artifacts"
    assert AnalysisArtifact.__table__.schema == "review"

    column_names = {column.name for column in AnalysisArtifact.__table__.columns}
    assert column_names == {
        "id",
        "artifact_uuid",
        "market",
        "kind",
        "title",
        "symbols",
        "payload",
        "as_of",
        "valid_until",
        "session_label",
        "created_by",
        "created_at",
    }

    constraints = AnalysisArtifact.__table__.constraints
    constraint_names = {constraint.name for constraint in constraints}
    assert "uq_analysis_artifacts_artifact_uuid" in constraint_names
    assert "ck_analysis_artifacts_market" in constraint_names
    assert "ck_analysis_artifacts_kind" in constraint_names
    assert "ck_analysis_artifacts_created_by" in constraint_names

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_analysis_artifacts_artifact_uuid"
        for constraint in constraints
    )
    assert any(isinstance(constraint, CheckConstraint) for constraint in constraints)

    indexes = AnalysisArtifact.__table__.indexes
    index_names = {index.name for index in indexes}
    assert {
        "ix_analysis_artifacts_kind_market_as_of",
        "ix_analysis_artifacts_symbols_gin",
        "ix_analysis_artifacts_payload_gin",
    }.issubset(index_names)
    assert any(
        isinstance(index, Index)
        and index.name == "ix_analysis_artifacts_symbols_gin"
        and index.dialect_options["postgresql"]["using"] == "gin"
        for index in indexes
    )
    assert any(
        isinstance(index, Index)
        and index.name == "ix_analysis_artifacts_payload_gin"
        and index.dialect_options["postgresql"]["using"] == "gin"
        for index in indexes
    )
