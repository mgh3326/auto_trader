"""add paper daily snapshots

Revision ID: 1666768ca8ff
Revises: 0a610ecdbacf
Create Date: 2026-04-13 16:18:56.616222

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1666768ca8ff'
down_revision: Union[str, Sequence[str], None] = '0a610ecdbacf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create paper.paper_daily_snapshots table."""
    op.create_table(
        'paper_daily_snapshots',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('snapshot_date', sa.Date(), nullable=False),
        sa.Column('cash_krw', sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column('cash_usd', sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column('positions_value', sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column('total_equity', sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column('daily_return_pct', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['account_id'],
            ['paper.paper_accounts.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'account_id', 'snapshot_date',
            name='uq_paper_daily_snapshots_account_date',
        ),
        schema='paper',
    )
    op.create_index(
        'ix_paper_daily_snapshots_account_date',
        'paper_daily_snapshots',
        ['account_id', 'snapshot_date'],
        schema='paper',
    )


def downgrade() -> None:
    """Drop paper.paper_daily_snapshots table."""
    op.drop_index(
        'ix_paper_daily_snapshots_account_date',
        table_name='paper_daily_snapshots',
        schema='paper',
    )
    op.drop_table('paper_daily_snapshots', schema='paper')
