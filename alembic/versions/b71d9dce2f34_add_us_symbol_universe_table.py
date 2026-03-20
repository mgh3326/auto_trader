from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b71d9dce2f34"
down_revision: str | Sequence[str] | None = "9a61b66981fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "us_symbol_universe",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("exchange", sa.String(length=10), nullable=False),
        sa.Column(
            "name_kr",
            sa.String(length=200),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "name_en",
            sa.String(length=200),
            nullable=False,
            server_default=sa.text("''"),
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
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_us_symbol_universe")),
    )
    op.create_index(
        "ix_us_symbol_universe_exchange_is_active",
        "us_symbol_universe",
        ["exchange", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_us_symbol_universe_exchange_is_active",
        table_name="us_symbol_universe",
    )
    op.drop_table("us_symbol_universe")
