"""Add paperclip_issue_id to trade_journals.

Revision ID: b8c4d2e0f1a9
Revises: b3f8a1c2d4e5
Create Date: 2026-04-15 18:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8c4d2e0f1a9"
down_revision: str | Sequence[str] | None = "b3f8a1c2d4e5"
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
