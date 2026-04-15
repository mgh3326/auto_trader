"""add sell_conditions table

Revision ID: b3f8a1c2d4e5
Revises: 00227d1d2890
Create Date: 2026-04-15 14:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f8a1c2d4e5"
down_revision: str = "00227d1d2890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sell_conditions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("price_threshold", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "stoch_rsi_threshold",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="80.0",
        ),
        sa.Column("foreign_days", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("rsi_high", sa.Numeric(6, 2), nullable=False, server_default="70.0"),
        sa.Column("rsi_low", sa.Numeric(6, 2), nullable=False, server_default="65.0"),
        sa.Column("bb_upper_ref", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sell_conditions"),
        sa.UniqueConstraint("symbol", name="uq_sell_conditions_symbol"),
    )
    op.create_index("ix_sell_conditions_symbol", "sell_conditions", ["symbol"])

    op.execute(
        """
        INSERT INTO sell_conditions (symbol, name, is_active, price_threshold,
            stoch_rsi_threshold, foreign_days, rsi_high, rsi_low, bb_upper_ref)
        VALUES ('000660', 'SK하이닉스', true, 1152000, 80.0, 2, 70.0, 65.0, 1142000)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_sell_conditions_symbol", table_name="sell_conditions")
    op.drop_table("sell_conditions")
