"""add caller_source to order history

Revision ID: c0e7a9d8f6b1
Revises: b8c4d2e0f1a9
Create Date: 2026-04-17 21:38:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0e7a9d8f6b1"
down_revision: str | Sequence[str] | None = "b8c4d2e0f1a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if _has_table("order_history") and not _has_column("order_history", "caller_source"):
        op.add_column(
            "order_history",
            sa.Column("caller_source", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    if _has_table("order_history") and _has_column("order_history", "caller_source"):
        op.drop_column("order_history", "caller_source")
