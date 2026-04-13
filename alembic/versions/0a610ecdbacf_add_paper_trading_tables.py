"""add paper trading tables

Revision ID: 0a610ecdbacf
Revises: 3e1f0c7a9b2d
Create Date: 2026-04-13 13:43:27.916878
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0a610ecdbacf"
down_revision: str | Sequence[str] | None = "3e1f0c7a9b2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing instrument_type enum — reuse, do not create
instrument_type_enum = postgresql.ENUM(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create paper schema
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS paper")

    # ------------------------------------------------------------------
    # 2. paper.paper_accounts
    # ------------------------------------------------------------------
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("initial_capital", sa.Numeric(20, 4), nullable=False),
        sa.Column("cash_krw", sa.Numeric(20, 4), nullable=False),
        sa.Column(
            "cash_usd",
            sa.Numeric(20, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("strategy_name", sa.String(length=128), nullable=True),
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
        sa.UniqueConstraint("name", name="uq_paper_accounts_name"),
        schema="paper",
    )

    # ------------------------------------------------------------------
    # 3. paper.paper_positions
    # ------------------------------------------------------------------
    op.create_table(
        "paper_positions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("avg_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("total_invested", sa.Numeric(20, 4), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper.paper_accounts.id"],
            ondelete="CASCADE",
            name="fk_paper_positions_account_id",
        ),
        sa.UniqueConstraint(
            "account_id", "symbol", name="uq_paper_positions_account_symbol"
        ),
        schema="paper",
    )
    op.create_index(
        "ix_paper_positions_account_id",
        "paper_positions",
        ["account_id"],
        schema="paper",
    )
    op.create_index(
        "ix_paper_positions_symbol",
        "paper_positions",
        ["symbol"],
        schema="paper",
    )

    # ------------------------------------------------------------------
    # 4. paper.paper_trades
    # ------------------------------------------------------------------
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("total_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("fee", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 4), nullable=True),
        sa.Column(
            "executed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper.paper_accounts.id"],
            ondelete="CASCADE",
            name="fk_paper_trades_account_id",
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="paper_trades_side"),
        sa.CheckConstraint(
            "order_type IN ('limit','market')", name="paper_trades_order_type"
        ),
        sa.CheckConstraint(
            "currency IN ('KRW','USD')", name="paper_trades_currency"
        ),
        schema="paper",
    )
    op.create_index(
        "ix_paper_trades_account_symbol",
        "paper_trades",
        ["account_id", "symbol"],
        schema="paper",
    )
    op.create_index(
        "ix_paper_trades_account_executed_at",
        "paper_trades",
        ["account_id", "executed_at"],
        schema="paper",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_trades_account_executed_at",
        table_name="paper_trades",
        schema="paper",
    )
    op.drop_index(
        "ix_paper_trades_account_symbol",
        table_name="paper_trades",
        schema="paper",
    )
    op.drop_table("paper_trades", schema="paper")

    op.drop_index(
        "ix_paper_positions_symbol",
        table_name="paper_positions",
        schema="paper",
    )
    op.drop_index(
        "ix_paper_positions_account_id",
        table_name="paper_positions",
        schema="paper",
    )
    op.drop_table("paper_positions", schema="paper")

    op.drop_table("paper_accounts", schema="paper")

    op.execute("DROP SCHEMA IF EXISTS paper")
