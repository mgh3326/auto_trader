"""ROB-745 allow codex created_by labels.

Revision ID: 20260706_rob745_codex_created_by
Revises: 20260706_rob734
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op


revision = "20260706_rob745_codex_created_by"
down_revision = "20260706_rob734"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_analysis_artifacts_created_by",
        "analysis_artifacts",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analysis_artifacts_created_by",
        "analysis_artifacts",
        "created_by IN ('claude','operator','system','codex')",
        schema="review",
    )
    op.drop_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        "created_by IN ('claude','operator','system','codex')",
        schema="review",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE review.analysis_artifacts "
        "SET created_by = 'claude' "
        "WHERE created_by = 'codex'"
    )
    op.execute(
        "UPDATE review.operator_session_context "
        "SET created_by = 'claude' "
        "WHERE created_by = 'codex'"
    )
    op.drop_constraint(
        "ck_analysis_artifacts_created_by",
        "analysis_artifacts",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analysis_artifacts_created_by",
        "analysis_artifacts",
        "created_by IN ('claude','operator','system')",
        schema="review",
    )
    op.drop_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        "created_by IN ('claude','operator','system')",
        schema="review",
    )
