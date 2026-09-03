"""Add isolated path-attribution metadata to the KIS mock order ledger.

Revision ID: 20260903_order_path_metadata
Revises: 20260902_rob1340_authority
Create Date: 2026-09-03 08:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260903_order_path_metadata"
down_revision = "20260902_rob1340_authority"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """Return whether the review-schema table already has ``column_name``.

    CI creates the ORM schema before it applies Alembic revisions, whereas a
    deployed database reaches this revision without the new ORM column.  The
    revision must support both bootstrap orders.
    """
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name, schema="review")
    )


def upgrade() -> None:
    if not _has_column("kis_mock_order_ledger", "extra_metadata"):
        op.add_column(
            "kis_mock_order_ledger",
            sa.Column(
                "extra_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
            schema="review",
        )


def downgrade() -> None:
    if _has_column("kis_mock_order_ledger", "extra_metadata"):
        op.drop_column("kis_mock_order_ledger", "extra_metadata", schema="review")
