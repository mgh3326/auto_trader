"""add invest screener snapshots

Revision ID: 82309c07b8a2
Revises: d2e3f4a5b6c7
Create Date: 2026-05-10 17:08:17.855667

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '82309c07b8a2'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create invest_screener_snapshots table."""
    op.create_table(
        'invest_screener_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('market', sa.String(8), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('snapshot_date', sa.Date(), nullable=False),
        sa.Column('latest_close', sa.Numeric(20, 6), nullable=False),
        sa.Column('prev_close', sa.Numeric(20, 6), nullable=True),
        sa.Column('change_amount', sa.Numeric(20, 6), nullable=True),
        sa.Column('change_rate', sa.Numeric(10, 4), nullable=True),
        sa.Column('consecutive_up_days', sa.Integer(), nullable=True),
        sa.Column('week_change_rate', sa.Numeric(10, 4), nullable=True),
        sa.Column('closes_window', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('daily_volume', sa.BigInteger(), nullable=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column(
            'computed_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.UniqueConstraint(
            'market',
            'symbol',
            'snapshot_date',
            name='uq_invest_screener_snapshots_market_symbol_date',
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us')",
            name='ck_invest_screener_snapshots_market',
        ),
        sa.CheckConstraint(
            "source IN ('kis', 'yahoo')",
            name='ck_invest_screener_snapshots_source',
        ),
    )

    op.create_index(
        'ix_invest_screener_snapshots_market_date',
        'invest_screener_snapshots',
        ['market', 'snapshot_date'],
    )

    op.create_index(
        'ix_invest_screener_snapshots_market_streak',
        'invest_screener_snapshots',
        ['market', 'consecutive_up_days'],
        postgresql_where=sa.text('consecutive_up_days IS NOT NULL'),
    )


def downgrade() -> None:
    """Drop invest_screener_snapshots table."""
    op.drop_index(
        'ix_invest_screener_snapshots_market_streak',
        table_name='invest_screener_snapshots',
        postgresql_where=sa.text('consecutive_up_days IS NOT NULL'),
    )
    op.drop_index(
        'ix_invest_screener_snapshots_market_date',
        table_name='invest_screener_snapshots',
    )
    op.drop_table('invest_screener_snapshots')
