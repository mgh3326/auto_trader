"""allow durable fill-event handoff provenance

Revision ID: 20260903_fill_event_handoff
Revises: 20260903_order_path_metadata
"""

from sqlalchemy import inspect

from alembic import op

revision = "20260903_fill_event_handoff"
down_revision = "20260903_order_path_metadata"
branch_labels = None
depends_on = None


def _created_by_constraint() -> tuple[str, str] | None:
    """Find the ORM-bootstrap or Alembic-created created_by CHECK by content."""
    constraints = inspect(op.get_bind()).get_check_constraints(
        "operator_session_context", schema="review"
    )
    for constraint in constraints:
        name = constraint.get("name")
        sqltext = constraint.get("sqltext")
        normalized = (
            sqltext.replace('"', "").lower() if isinstance(sqltext, str) else ""
        )
        if (
            isinstance(name, str)
            and "created_by" in normalized
            and (" in " in normalized or "any" in normalized)
        ):
            return name, sqltext
    return None


def upgrade() -> None:
    existing = _created_by_constraint()
    if existing is not None and "fill-event-handoff" in existing[1]:
        return
    if existing is not None:
        op.drop_constraint(
            op.f(existing[0]),
            "operator_session_context",
            schema="review",
            type_="check",
        )
    op.create_check_constraint(
        op.f("ck_operator_session_context_created_by"),
        "operator_session_context",
        "created_by IN ('claude','operator','system','codex','fill-event-handoff')",
        schema="review",
    )


def downgrade() -> None:
    existing = _created_by_constraint()
    if existing is None or "fill-event-handoff" not in existing[1]:
        return
    # The prior CHECK cannot accept a handoff row.  Preserve the durable row
    # while returning it to a provenance label valid at the parent revision.
    op.execute(
        "UPDATE review.operator_session_context "
        "SET created_by = 'system' "
        "WHERE created_by = 'fill-event-handoff'"
    )
    op.drop_constraint(
        op.f(existing[0]),
        "operator_session_context",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_operator_session_context_created_by"),
        "operator_session_context",
        "created_by IN ('claude','operator','system','codex')",
        schema="review",
    )
