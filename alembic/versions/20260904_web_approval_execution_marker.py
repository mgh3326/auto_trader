"""add web approval execution marker states

Revision ID: 20260904_web_approval_marker
Revises: 20260903_fill_event_handoff
"""

import sqlalchemy as sa

from alembic import op

revision = "20260904_web_approval_marker"
down_revision = "20260903_fill_event_handoff"
branch_labels = None
depends_on = None

_TABLE = "order_proposal_approval_events"
_SCHEMA = "review"


def upgrade() -> None:
    # This append-only ledger already holds opaque ceremony digests.  Extend
    # its constrained vocabulary for a web-only handler-entered marker and a
    # terminal record; no raw request token, exception, or browser payload is
    # persisted.
    op.drop_constraint(
        "order_proposal_approval_events_step", _TABLE, schema=_SCHEMA, type_="check"
    )
    op.drop_constraint(
        "order_proposal_approval_events_outcome", _TABLE, schema=_SCHEMA, type_="check"
    )
    op.create_check_constraint(
        "order_proposal_approval_events_step",
        _TABLE,
        "step IN ('begin','confirm','handler_entered','terminal')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "order_proposal_approval_events_outcome",
        _TABLE,
        "outcome IN ('accepted','rejected','needs_reconfirm','expired','entered',"
        "'completed','dead_letter')",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    # A downgrade must not discard durable execution evidence.  The prior
    # narrower checks cannot represent marker rows, so only permit this schema
    # reversal when none exists (the isolated migration-chain test exercises
    # that empty path).
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM review.order_proposal_approval_events "
                "WHERE step IN ('handler_entered', 'terminal') "
                "OR outcome IN ('entered', 'completed', 'dead_letter')"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError("web approval execution marker rows are append-only")
    op.drop_constraint(
        "order_proposal_approval_events_step", _TABLE, schema=_SCHEMA, type_="check"
    )
    op.drop_constraint(
        "order_proposal_approval_events_outcome", _TABLE, schema=_SCHEMA, type_="check"
    )
    op.create_check_constraint(
        "order_proposal_approval_events_step",
        _TABLE,
        "step IN ('begin','confirm')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "order_proposal_approval_events_outcome",
        _TABLE,
        "outcome IN ('accepted','rejected','needs_reconfirm','expired')",
        schema=_SCHEMA,
    )
