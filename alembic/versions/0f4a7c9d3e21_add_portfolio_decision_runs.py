"""add portfolio decision run snapshots

Revision ID: 0f4a7c9d3e21
Revises: c0e7a9d8f6b1
Create Date: 2026-04-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0f4a7c9d3e21"
down_revision: str | Sequence[str] | None = "c0e7a9d8f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_decision_runs",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market_scope", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("facets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "symbol_groups", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_portfolio_decision_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_portfolio_decision_runs")),
    )
    op.create_index(
        op.f("ix_portfolio_decision_runs_user_id"),
        "portfolio_decision_runs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_portfolio_decision_runs_generated_at"),
        "portfolio_decision_runs",
        ["generated_at"],
        unique=False,
    )
    op.create_index(
        "ix_portfolio_decision_runs_user_generated_at",
        "portfolio_decision_runs",
        ["user_id", "generated_at"],
        unique=False,
    )
    op.create_index(
        "ix_portfolio_decision_runs_market_scope",
        "portfolio_decision_runs",
        ["market_scope"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_decision_runs_market_scope",
        table_name="portfolio_decision_runs",
    )
    op.drop_index(
        "ix_portfolio_decision_runs_user_generated_at",
        table_name="portfolio_decision_runs",
    )
    op.drop_index(
        op.f("ix_portfolio_decision_runs_generated_at"),
        table_name="portfolio_decision_runs",
    )
    op.drop_index(
        op.f("ix_portfolio_decision_runs_user_id"),
        table_name="portfolio_decision_runs",
    )
    op.drop_table("portfolio_decision_runs")
