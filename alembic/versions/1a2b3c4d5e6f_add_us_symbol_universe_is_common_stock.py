"""add us_symbol_universe is_common_stock flag

Revision ID: 1a2b3c4d5e6f
Revises: 9f1a2b3c4d5e
Create Date: 2026-05-12 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | Sequence[str] | None = "9f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "us_symbol_universe",
        sa.Column("is_common_stock", sa.Boolean(), nullable=True),
    )
    op.create_index(
        "ix_us_symbol_universe_common_active_symbol",
        "us_symbol_universe",
        ["is_common_stock", "is_active", "symbol"],
        unique=False,
        postgresql_where=sa.text("is_common_stock IS TRUE AND is_active IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_us_symbol_universe_common_active_symbol",
        table_name="us_symbol_universe",
        postgresql_where=sa.text("is_common_stock IS TRUE AND is_active IS TRUE"),
    )
    op.drop_column("us_symbol_universe", "is_common_stock")
