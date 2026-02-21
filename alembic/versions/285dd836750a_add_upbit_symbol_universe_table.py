"""add_upbit_symbol_universe_table

Revision ID: 285dd836750a
Revises: b71d9dce2f34
Create Date: 2026-02-21 04:01:21.651965

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "285dd836750a"
down_revision: str | Sequence[str] | None = "b71d9dce2f34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upbit_symbol_universe",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("korean_name", sa.String(length=100), nullable=False),
        sa.Column("english_name", sa.String(length=100), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column(
            "market_warning",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'NONE'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_upbit_symbol_universe")),
    )
    op.create_index(
        "ix_upbit_symbol_universe_market_is_active",
        "upbit_symbol_universe",
        ["market", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_upbit_symbol_universe_market_is_active",
        table_name="upbit_symbol_universe",
    )
    op.drop_table("upbit_symbol_universe")
