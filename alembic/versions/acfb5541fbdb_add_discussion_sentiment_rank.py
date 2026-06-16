"""add discussion sentiment rank to investor flow snapshots

Revision ID: acfb5541fbdb
Revises: 885e50ac5bb1
Create Date: 2026-06-16 13:55:11.678920

ROB-586: ``foreign_holding_shares`` / ``foreign_holding_rate`` are already added
by ``20260615_rob575_investor_flow_snapshot_market_fields`` (merged separately),
so this migration only adds the discussion-sentiment rank column.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "acfb5541fbdb"
down_revision: Union[str, Sequence[str], None] = "885e50ac5bb1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("discussion_sentiment_rank", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("investor_flow_snapshots", "discussion_sentiment_rank")
