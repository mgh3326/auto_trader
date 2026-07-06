"""ROB-743 mirror idempotency unique index

Revision ID: 20260706_rob743_mirror_idempotency
Revises: 20260706_rob734_mirror_counterfactual
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260706_rob743"
down_revision = "20260706_rob734"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ux_kis_mock_mirror_report_item_once",
        "kis_mock_order_ledger",
        ["mirror_cohort", "report_item_uuid"],
        unique=True,
        schema="review",
        postgresql_where=sa.text(
            "mirror_cohort = 'mock_counterfactual' AND report_item_uuid IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_kis_mock_mirror_report_item_once",
        table_name="kis_mock_order_ledger",
        schema="review",
    )
