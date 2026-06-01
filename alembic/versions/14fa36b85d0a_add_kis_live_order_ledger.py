"""add kis_live_order_ledger

Revision ID: 14fa36b85d0a
Revises: 20260529_rob352
Create Date: 2026-06-01 12:08:48.800868

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '14fa36b85d0a'
down_revision: Union[str, Sequence[str], None] = '20260529_rob352'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('kis_live_order_ledger',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('trade_date', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('symbol', sa.Text(), nullable=False),
    sa.Column('instrument_type', sa.Text(), nullable=False),
    sa.Column('side', sa.Text(), nullable=False),
    sa.Column('order_type', sa.Text(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('price', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('fee', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('currency', sa.Text(), nullable=True),
    sa.Column('order_no', sa.Text(), nullable=True),
    sa.Column('order_time', sa.Text(), nullable=True),
    sa.Column('krx_fwdg_ord_orgno', sa.Text(), nullable=True),
    sa.Column('account_mode', sa.Text(), nullable=False),
    sa.Column('broker', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('lifecycle_state', sa.Text(), nullable=False),
    sa.Column('response_code', sa.Text(), nullable=True),
    sa.Column('response_message', sa.Text(), nullable=True),
    sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('thesis', sa.Text(), nullable=True),
    sa.Column('strategy', sa.Text(), nullable=True),
    sa.Column('target_price', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('stop_loss', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('min_hold_days', sa.SmallInteger(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('exit_reason', sa.Text(), nullable=True),
    sa.Column('indicators_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('filled_qty', sa.Numeric(precision=20, scale=8), nullable=True),
    sa.Column('avg_fill_price', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('trade_id', sa.BigInteger(), nullable=True),
    sa.Column('journal_id', sa.BigInteger(), nullable=True),
    sa.Column('reconciled_at', sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_kis_live_order_ledger')),
    sa.UniqueConstraint('order_no', name='uq_kis_live_ledger_order_no'),
    schema='review'
    )
    op.create_index('ix_kis_live_ledger_status', 'kis_live_order_ledger', ['status'], unique=False, schema='review')
    op.create_index('ix_kis_live_ledger_symbol', 'kis_live_order_ledger', ['symbol'], unique=False, schema='review')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_kis_live_ledger_symbol', table_name='kis_live_order_ledger', schema='review')
    op.drop_index('ix_kis_live_ledger_status', table_name='kis_live_order_ledger', schema='review')
    op.drop_table('kis_live_order_ledger', schema='review')
