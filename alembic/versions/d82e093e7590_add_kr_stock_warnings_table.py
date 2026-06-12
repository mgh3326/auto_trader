"""add kr_stock_warnings table

Revision ID: d82e093e7590
Revises: 20260612_rob534
Create Date: 2026-06-12 16:49:08.301279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd82e093e7590'
down_revision: Union[str, Sequence[str], None] = '20260612_rob534'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('kr_stock_warnings',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('market', sa.String(length=10), nullable=False),
    sa.Column('symbol', sa.String(length=10), nullable=False),
    sa.Column('warning_type', sa.String(length=50), nullable=False),
    sa.Column('exchange', sa.String(length=20), nullable=True),
    sa.Column('start_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('fetched_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_kr_stock_warnings'))
    )
    op.create_index('ix_kr_stock_warnings_market_symbol', 'kr_stock_warnings', ['market', 'symbol'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_kr_stock_warnings_market_symbol', table_name='kr_stock_warnings')
    op.drop_table('kr_stock_warnings')
