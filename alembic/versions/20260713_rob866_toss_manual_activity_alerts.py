"""ROB-866 — additive review.toss_manual_activity_alerts idempotency marker.

Alert-only marker for the Toss manual-activity detection sweep. Presence of a
broker_order_id means "already alerted; do not re-alert". This is NOT a
fill/bookkeeping ledger (that is stage 2). Additive-only; no existing table is
touched. Operator runs ``alembic upgrade head`` separately at cutover.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260713_rob866_toss_manual_activity"
down_revision = "20260713_rob844_root_reservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "toss_manual_activity_alerts",
        sa.Column("broker_order_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column(
            "is_open",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "alerted_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "broker_order_id", name="pk_toss_manual_activity_alerts"
        ),
        schema="review",
    )
    op.create_index(
        "ix_toss_manual_activity_alerts_alerted_at",
        "toss_manual_activity_alerts",
        ["alerted_at"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_toss_manual_activity_alerts_alerted_at",
        table_name="toss_manual_activity_alerts",
        schema="review",
    )
    op.drop_table("toss_manual_activity_alerts", schema="review")
