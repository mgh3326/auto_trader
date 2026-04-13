"""add account_type and paper_trade_id to trade_journals

Revision ID: db555723284f
Revises: 0a610ecdbacf
Create Date: 2026-04-13 18:28:11.653257

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db555723284f'
down_revision: Union[str, Sequence[str], None] = '0a610ecdbacf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_journals",
        sa.Column(
            "account_type",
            sa.Text(),
            nullable=False,
            server_default="live",
        ),
        schema="review",
    )
    op.add_column(
        "trade_journals",
        sa.Column("paper_trade_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "trade_journals_account_type",
        "trade_journals",
        "account_type IN ('live','paper')",
        schema="review",
    )
    op.create_check_constraint(
        "trade_journals_no_paper_trade_on_live",
        "trade_journals",
        "NOT (account_type = 'live' AND paper_trade_id IS NOT NULL)",
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_account_type",
        "trade_journals",
        ["account_type"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journals_account_type",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_constraint(
        "trade_journals_no_paper_trade_on_live",
        "trade_journals",
        schema="review",
    )
    op.drop_constraint(
        "trade_journals_account_type",
        "trade_journals",
        schema="review",
    )
    op.drop_column("trade_journals", "paper_trade_id", schema="review")
    op.drop_column("trade_journals", "account_type", schema="review")
