"""ROB-473 — add report_item_uuid to live order ledgers (audit linkage)

Revision ID: 20260609_rob473
Revises: 20260609_rob455
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision = "20260609_rob473"
down_revision = "20260609_rob455"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("kis_live_order_ledger", "live_order_ledger"):
        op.add_column(
            table,
            sa.Column("report_item_uuid", PG_UUID(as_uuid=True), nullable=True),
            schema="review",
        )
    op.create_index(
        "ix_kis_live_ledger_report_item_uuid",
        "kis_live_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_live_ledger_report_item_uuid",
        "live_order_ledger",
        ["report_item_uuid"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_ledger_report_item_uuid",
        table_name="live_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_kis_live_ledger_report_item_uuid",
        table_name="kis_live_order_ledger",
        schema="review",
    )
    for table in ("live_order_ledger", "kis_live_order_ledger"):
        op.drop_column(table, "report_item_uuid", schema="review")
