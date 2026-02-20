from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a61b66981fe"
down_revision: str | Sequence[str] | None = "f2c1e9b7a4d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kr_symbol_universe",
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("exchange", sa.String(length=10), nullable=False),
        sa.Column(
            "nxt_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_kr_symbol_universe")),
    )
    op.create_index(
        "ix_kr_symbol_universe_exchange_is_active_nxt",
        "kr_symbol_universe",
        ["exchange", "is_active", "nxt_eligible"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kr_symbol_universe_exchange_is_active_nxt",
        table_name="kr_symbol_universe",
    )
    op.drop_table("kr_symbol_universe")
