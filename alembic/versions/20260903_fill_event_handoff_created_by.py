"""allow durable fill-event handoff provenance

Revision ID: 20260903_fill_event_handoff
Revises: 20260902_rob1340_authority
"""

from alembic import op

revision = "20260903_fill_event_handoff"
down_revision = "20260902_rob1340_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operator_session_context_created_by",
        "operator_session_context",
        "created_by IN ('claude','operator','system','codex','fill-event-handoff')",
        schema="review",
    )


def downgrade() -> None:
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
