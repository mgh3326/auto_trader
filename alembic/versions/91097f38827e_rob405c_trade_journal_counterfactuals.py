"""rob405c trade_journal_counterfactuals

Revision ID: 91097f38827e
Revises: 6e1ed781fc56
Create Date: 2026-06-02 07:32:13.334954

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '91097f38827e'
down_revision: Union[str, Sequence[str], None] = '6e1ed781fc56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_journal_counterfactuals",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("trigger_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("triggered_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("no_action_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("no_action_as_of", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("fill_vs_trigger_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("no_action_vs_fill_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_trade_journal_counterfactuals_correlation_id"
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journal_counterfactuals_journal_id",
        "trade_journal_counterfactuals",
        ["journal_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journal_counterfactuals_journal_id",
        "trade_journal_counterfactuals",
        schema="review",
    )
    op.drop_table("trade_journal_counterfactuals", schema="review")

