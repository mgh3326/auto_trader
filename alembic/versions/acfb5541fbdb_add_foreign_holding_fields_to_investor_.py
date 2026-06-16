"""add foreign holding fields to investor flow snapshots

Revision ID: acfb5541fbdb
Revises: 20260615_rob568_us_fx_pnl
Create Date: 2026-06-16 13:55:11.678920

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'acfb5541fbdb'
down_revision: Union[str, Sequence[str], None] = '20260615_rob568_us_fx_pnl'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("investor_flow_snapshots", sa.Column("foreign_holding_shares", sa.BigInteger(), nullable=True))
    op.add_column("investor_flow_snapshots", sa.Column("foreign_holding_rate", sa.Numeric(precision=10, scale=4), nullable=True))
    op.add_column("investor_flow_snapshots", sa.Column("discussion_sentiment_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("investor_flow_snapshots", "discussion_sentiment_rank")
    op.drop_column("investor_flow_snapshots", "foreign_holding_rate")
    op.drop_column("investor_flow_snapshots", "foreign_holding_shares")
