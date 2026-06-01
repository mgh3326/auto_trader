"""rob405b trade_journal_reviews

Revision ID: 6e1ed781fc56
Revises: 075d44e505b9
Create Date: 2026-06-02 06:52:39.363719

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e1ed781fc56'
down_revision: Union[str, Sequence[str], None] = '075d44e505b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_journal_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("journal_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("verdict_source", sa.Text(), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="ck_trade_journal_reviews_verdict",
        ),
        sa.CheckConstraint(
            "verdict_source IN ('auto','manual')",
            name="ck_trade_journal_reviews_source",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journal_reviews_journal_id",
        "trade_journal_reviews",
        ["journal_id"],
        schema="review",
    )
    op.create_index(
        "uq_trade_journal_reviews_auto",
        "trade_journal_reviews",
        ["journal_id"],
        schema="review",
        unique=True,
        postgresql_where=sa.text("verdict_source = 'auto'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_trade_journal_reviews_auto", "trade_journal_reviews", schema="review"
    )
    op.drop_index(
        "ix_trade_journal_reviews_journal_id", "trade_journal_reviews", schema="review"
    )
    op.drop_table("trade_journal_reviews", schema="review")

