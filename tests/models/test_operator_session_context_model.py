from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.models.session_context import OperatorSessionContext


def test_operator_session_context_model_contract() -> None:
    assert OperatorSessionContext.__tablename__ == "operator_session_context"
    assert OperatorSessionContext.__table__.schema == "review"

    column_names = {column.name for column in OperatorSessionContext.__table__.columns}
    assert column_names == {
        "id",
        "entry_uuid",
        "kst_date",
        "market",
        "account_scope",
        "entry_type",
        "title",
        "body",
        "refs",
        "created_by",
        "session_label",
        "created_at",
    }

    constraints = OperatorSessionContext.__table__.constraints
    constraint_names = {constraint.name for constraint in constraints}
    assert "uq_operator_session_context_entry_uuid" in constraint_names
    assert "ck_operator_session_context_market" in constraint_names
    assert "ck_operator_session_context_account_scope" in constraint_names
    assert "ck_operator_session_context_entry_type" in constraint_names
    assert "ck_operator_session_context_created_by" in constraint_names
    assert "ck_operator_session_context_refs_object" in constraint_names

    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_operator_session_context_entry_uuid"
        for constraint in constraints
    )
    assert any(isinstance(constraint, CheckConstraint) for constraint in constraints)

    indexes = OperatorSessionContext.__table__.indexes
    index_names = {index.name for index in indexes}
    assert {
        "ix_operator_session_context_market_date_created",
        "ix_operator_session_context_entry_type_date",
        "ix_operator_session_context_refs_gin",
    }.issubset(index_names)
    assert any(
        isinstance(index, Index)
        and index.name == "ix_operator_session_context_refs_gin"
        and index.dialect_options["postgresql"]["using"] == "gin"
        for index in indexes
    )
