"""add paperclip_issue_id to trade_journals

Revision ID: a1b2c3d4e5f6
Revises: 00227d1d2890
Create Date: 2026-04-15 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "00227d1d2890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_journals",
        sa.Column("paperclip_issue_id", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_paperclip_issue_id",
        "trade_journals",
        ["paperclip_issue_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journals_paperclip_issue_id",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_column("trade_journals", "paperclip_issue_id", schema="review")
