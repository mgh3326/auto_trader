"""ROB-1286 durable watch-event repricing claims (additive)

Revision ID: 20260819_rob1286_claims
Revises: 20260815_funding_advisory
Create Date: 2026-08-19

Additive: creates one new table in the ``review`` schema. No existing table,
column, constraint or index is altered, and ``review.investment_watch_events``
is not touched -- this feature reads it and never writes it.

Provenance: produced by ``alembic revision --autogenerate`` against a
run-owned scratch database, then reduced to
this table. The raw autogenerate output additionally proposed ~3300 lines of
unrelated ``create_table`` calls, which is pre-existing drift between the ORM
metadata and the migration chain (tables other features create elsewhere) and
is emphatically not this migration's business to "fix". The kept block is the
generated one verbatim.

The three constraints that carry the concurrency safety are described on
``app/models/watch_event_repricing_claims.WatchEventRepricingClaim``. In
short: ``UNIQUE (event_uuid, generation)`` makes a generation claimable once,
the partial unique index makes "one live session per symbol" a database
property rather than a snapshot read, and ``owner_token`` + ``generation``
fence a stale claimant out of the current row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260819_rob1286_claims"
down_revision: str | Sequence[str] | None = "20260815_funding_advisory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create review.watch_event_repricing_claims."""
    op.create_table(
        "watch_event_repricing_claims",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event_uuid", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column(
            "generation", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("owner_token", sa.UUID(), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=False),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'started'"), nullable=False
        ),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "claimed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("finalised_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state <> 'proposal_created' OR "
            " (proposal_id IS NOT NULL AND length(btrim(proposal_id)) > 0)) AND "
            "(state <> 'rejected_with_reason' OR "
            " (rejection_reason IS NOT NULL AND "
            "  length(btrim(rejection_reason)) > 0))",
            name=op.f("ck_watch_event_repricing_claims_terminal_evidence"),
        ),
        sa.CheckConstraint(
            "state IN ('started', 'proposal_created', 'rejected_with_reason', "
            "'expired_unprocessed')",
            name=op.f("ck_watch_event_repricing_claims_state"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_watch_event_repricing_claims_generation"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_event_repricing_claims")),
        sa.UniqueConstraint(
            "event_uuid",
            "generation",
            name="uq_watch_event_repricing_claims_event_generation",
        ),
        schema="review",
    )
    op.create_index(
        "ix_watch_event_repricing_claims_event",
        "watch_event_repricing_claims",
        ["event_uuid"],
        unique=False,
        schema="review",
    )
    op.create_index(
        "ix_watch_event_repricing_claims_state_lease",
        "watch_event_repricing_claims",
        ["state", "lease_expires_at"],
        unique=False,
        schema="review",
    )
    op.create_index(
        "uq_watch_event_repricing_claims_active_symbol",
        "watch_event_repricing_claims",
        ["symbol"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("state = 'started'"),
    )


def downgrade() -> None:
    """Drop review.watch_event_repricing_claims."""
    op.drop_index(
        "uq_watch_event_repricing_claims_active_symbol",
        table_name="watch_event_repricing_claims",
        schema="review",
        postgresql_where=sa.text("state = 'started'"),
    )
    op.drop_index(
        "ix_watch_event_repricing_claims_state_lease",
        table_name="watch_event_repricing_claims",
        schema="review",
    )
    op.drop_index(
        "ix_watch_event_repricing_claims_event",
        table_name="watch_event_repricing_claims",
        schema="review",
    )
    op.drop_table("watch_event_repricing_claims", schema="review")
