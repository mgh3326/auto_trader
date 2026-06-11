"""ROB-512 Add symbol_sectors and universe sector FK columns

Revision ID: rob512_symbol_sectors
Revises: 20260610_rob491
Create Date: 2026-06-11 10:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "rob512_symbol_sectors"
down_revision = "20260610_rob491"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create symbol_sectors table
    op.create_table(
        "symbol_sectors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=100), nullable=False),
        sa.Column("name_kr", sa.String(length=100), nullable=True),
        sa.Column("name_en", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market", "source", "source_key", name="uq_symbol_sectors_market_source_key"
        ),
    )

    # 2. Add columns to kr_symbol_universe
    op.add_column(
        "kr_symbol_universe", sa.Column("sector_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "kr_symbol_universe",
        sa.Column("sector_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_kr_symbol_universe_sector_id",
        "kr_symbol_universe",
        "symbol_sectors",
        ["sector_id"],
        ["id"],
    )

    # 3. Add columns to us_symbol_universe
    op.add_column(
        "us_symbol_universe", sa.Column("sector_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "us_symbol_universe",
        sa.Column("sector_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_us_symbol_universe_sector_id",
        "us_symbol_universe",
        "symbol_sectors",
        ["sector_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_us_symbol_universe_sector_id", "us_symbol_universe", type_="foreignkey"
    )
    op.drop_column("us_symbol_universe", "sector_updated_at")
    op.drop_column("us_symbol_universe", "sector_id")

    op.drop_constraint(
        "fk_kr_symbol_universe_sector_id", "kr_symbol_universe", type_="foreignkey"
    )
    op.drop_column("kr_symbol_universe", "sector_updated_at")
    op.drop_column("kr_symbol_universe", "sector_id")

    op.drop_table("symbol_sectors")
