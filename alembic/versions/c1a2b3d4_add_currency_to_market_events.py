"""add currency column to market_events (ROB-132)

Revision ID: c1a2b3d4
Revises: a7e9c128
Create Date: 2026-05-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1a2b3d4"
down_revision: str | Sequence[str] | None = "a7e9c128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_events",
        sa.Column("currency", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_events", "currency")
