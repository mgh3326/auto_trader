"""add kis_mock_order_ledger lifecycle columns (ROB-102)

Revision ID: d4f1c5a3e2b1
Revises: d4e5f6a7b8c9
Create Date: 2026-05-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4f1c5a3e2b1"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LIFECYCLE_STATES = (
    "planned",
    "previewed",
    "submitted",
    "accepted",
    "pending",
    "fill",
    "reconciled",
    "stale",
    "failed",
    "anomaly",
)


def upgrade() -> None:
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column(
            "lifecycle_state",
            sa.Text(),
            nullable=False,
            server_default="anomaly",
        ),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column("holdings_baseline_qty", sa.Numeric(20, 8), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column(
            "reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        schema="review",
    )
    op.add_column(
        "kis_mock_order_ledger",
        sa.Column(
            "last_reconcile_detail", postgresql.JSONB(), nullable=True
        ),
        schema="review",
    )

    op.execute(
        """
        UPDATE review.kis_mock_order_ledger
        SET lifecycle_state = CASE status
            WHEN 'accepted' THEN 'accepted'
            WHEN 'rejected' THEN 'failed'
            ELSE 'anomaly'
        END
        """
    )

    states = ", ".join(f"'{s}'" for s in _LIFECYCLE_STATES)
    op.create_check_constraint(
        "kis_mock_ledger_lifecycle_state_allowed",
        "kis_mock_order_ledger",
        f"lifecycle_state IN ({states})",
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_ledger_lifecycle_state",
        "kis_mock_order_ledger",
        ["lifecycle_state"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kis_mock_ledger_lifecycle_state",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
    op.drop_constraint(
        "kis_mock_ledger_lifecycle_state_allowed",
        "kis_mock_order_ledger",
        schema="review",
        type_="check",
    )
    op.drop_column(
        "kis_mock_order_ledger", "last_reconcile_detail", schema="review"
    )
    op.drop_column(
        "kis_mock_order_ledger", "reconciled_at", schema="review"
    )
    op.drop_column(
        "kis_mock_order_ledger", "reconcile_attempts", schema="review"
    )
    op.drop_column(
        "kis_mock_order_ledger", "holdings_baseline_qty", schema="review"
    )
    op.drop_column("kis_mock_order_ledger", "lifecycle_state", schema="review")
