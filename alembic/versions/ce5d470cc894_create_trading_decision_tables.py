"""create trading decision tables

Revision ID: ce5d470cc894
Revises: 0f4a7c9d3e21
Create Date: 2026-04-27 14:42:02.817288

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ce5d470cc894'
down_revision: str | Sequence[str] | None = '0f4a7c9d3e21'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing instrument_type enum — reuse, do not create
instrument_type_enum = postgresql.ENUM(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    """Upgrade schema."""
    # 1. trading_decision_sessions
    op.create_table(
        'trading_decision_sessions',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('session_uuid', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('source_profile', sa.Text(), nullable=False),
        sa.Column('strategy_name', sa.Text(), nullable=True),
        sa.Column('market_scope', sa.Text(), nullable=True),
        sa.Column('market_brief', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='open'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('generated_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('open', 'closed', 'archived')", name='trading_decision_sessions_status_allowed'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_uuid')
    )
    op.create_index('ix_trading_decision_sessions_user_generated_at', 'trading_decision_sessions', ['user_id', sa.text('generated_at DESC')], postgresql_using='btree')
    op.create_index(op.f('ix_trading_decision_sessions_session_uuid'), 'trading_decision_sessions', ['session_uuid'], unique=True)
    op.create_index(op.f('ix_trading_decision_sessions_user_id'), 'trading_decision_sessions', ['user_id'], unique=False)
    op.create_foreign_key(None, 'trading_decision_sessions', 'users', ['user_id'], ['id'], ondelete='CASCADE')

    # 2. trading_decision_proposals
    op.create_table(
        'trading_decision_proposals',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('proposal_uuid', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('session_id', sa.BigInteger(), nullable=False),
        sa.Column('symbol', sa.Text(), nullable=False),
        sa.Column('instrument_type', instrument_type_enum, nullable=False),
        sa.Column('proposal_kind', sa.Text(), nullable=False),
        sa.Column('side', sa.Text(), nullable=False, server_default='none'),
        sa.Column('original_quantity', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('original_quantity_pct', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('original_amount', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('original_price', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('original_trigger_price', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('original_threshold_pct', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('original_currency', sa.Text(), nullable=True),
        sa.Column('original_rationale', sa.Text(), nullable=True),
        sa.Column('original_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('user_response', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('user_quantity', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('user_quantity_pct', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('user_amount', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('user_price', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('user_trigger_price', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('user_threshold_pct', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('user_note', sa.Text(), nullable=True),
        sa.Column('responded_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("proposal_kind IN ('trim','add','enter','exit','pullback_watch','breakout_watch','avoid','no_action','other')", name='trading_decision_proposals_kind_allowed'),
        sa.CheckConstraint("side IN ('buy','sell','none')", name='trading_decision_proposals_side_allowed'),
        sa.CheckConstraint("user_response IN ('pending','accept','reject','modify','partial_accept','defer')", name='trading_decision_proposals_user_response_allowed'),
        sa.CheckConstraint("(user_response = 'pending') = (responded_at IS NULL)", name='trading_decision_proposals_pending_response_invariant'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('proposal_uuid')
    )
    op.create_index(op.f('ix_trading_decision_proposals_proposal_uuid'), 'trading_decision_proposals', ['proposal_uuid'], unique=True)
    op.create_index(op.f('ix_trading_decision_proposals_session_id'), 'trading_decision_proposals', ['session_id'], unique=False)
    op.create_index(op.f('ix_trading_decision_proposals_symbol'), 'trading_decision_proposals', ['symbol'], unique=False)
    op.create_index(op.f('ix_trading_decision_proposals_user_response'), 'trading_decision_proposals', ['user_response'], unique=False)
    op.create_index('ix_trading_decision_proposals_session_response', 'trading_decision_proposals', ['session_id', 'user_response'], unique=False)
    op.create_foreign_key(None, 'trading_decision_proposals', 'trading_decision_sessions', ['session_id'], ['id'], ondelete='CASCADE')

    # 3. trading_decision_actions
    op.create_table(
        'trading_decision_actions',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('proposal_id', sa.BigInteger(), nullable=False),
        sa.Column('action_kind', sa.Text(), nullable=False),
        sa.Column('external_order_id', sa.Text(), nullable=True),
        sa.Column('external_paper_id', sa.Text(), nullable=True),
        sa.Column('external_watch_id', sa.Text(), nullable=True),
        sa.Column('external_source', sa.Text(), nullable=True),
        sa.Column('payload_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('recorded_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("action_kind IN ('live_order','paper_order','watch_alert','no_action','manual_note')", name='trading_decision_actions_kind_allowed'),
        sa.CheckConstraint("(action_kind IN ('no_action', 'manual_note')) OR (external_order_id IS NOT NULL OR external_paper_id IS NOT NULL OR external_watch_id IS NOT NULL)", name='trading_decision_actions_external_id_required'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trading_decision_actions_proposal_id'), 'trading_decision_actions', ['proposal_id'], unique=False)
    op.create_index('ix_trading_decision_actions_external_order', 'trading_decision_actions', ['external_source', 'external_order_id'], unique=False, postgresql_where=sa.text('external_order_id IS NOT NULL'))
    op.create_foreign_key(None, 'trading_decision_actions', 'trading_decision_proposals', ['proposal_id'], ['id'], ondelete='CASCADE')

    # 4. trading_decision_counterfactuals
    op.create_table(
        'trading_decision_counterfactuals',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('proposal_id', sa.BigInteger(), nullable=False),
        sa.Column('track_kind', sa.Text(), nullable=False),
        sa.Column('baseline_price', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('baseline_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("track_kind IN ('rejected_counterfactual','analyst_alternative','user_alternative','accepted_paper')", name='trading_decision_counterfactuals_kind_allowed'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trading_decision_counterfactuals_proposal_id'), 'trading_decision_counterfactuals', ['proposal_id'], unique=False)
    op.create_foreign_key(None, 'trading_decision_counterfactuals', 'trading_decision_proposals', ['proposal_id'], ['id'], ondelete='CASCADE')

    # 5. trading_decision_outcomes
    op.create_table(
        'trading_decision_outcomes',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('proposal_id', sa.BigInteger(), nullable=False),
        sa.Column('counterfactual_id', sa.BigInteger(), nullable=True),
        sa.Column('track_kind', sa.Text(), nullable=False),
        sa.Column('horizon', sa.Text(), nullable=False),
        sa.Column('price_at_mark', sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column('pnl_pct', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('pnl_amount', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('marked_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("track_kind IN ('accepted_live','accepted_paper','rejected_counterfactual','analyst_alternative','user_alternative')", name='trading_decision_outcomes_track_kind_allowed'),
        sa.CheckConstraint("horizon IN ('1h','4h','1d','3d','7d','final')", name='trading_decision_outcomes_horizon_allowed'),
        sa.CheckConstraint("(track_kind = 'accepted_live') = (counterfactual_id IS NULL)", name='trading_decision_outcomes_accepted_live_requires_null_counterfactual'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trading_decision_outcomes_proposal_id'), 'trading_decision_outcomes', ['proposal_id'], unique=False)
    op.create_index('ix_trading_decision_outcomes_track_identity', 'trading_decision_outcomes', ['proposal_id', 'counterfactual_id', 'track_kind', 'horizon'], unique=True, postgresql_nulls_not_distinct=True)
    op.create_foreign_key(None, 'trading_decision_outcomes', 'trading_decision_proposals', ['proposal_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key(None, 'trading_decision_outcomes', 'trading_decision_counterfactuals', ['counterfactual_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('trading_decision_outcomes')
    op.drop_table('trading_decision_counterfactuals')
    op.drop_table('trading_decision_actions')
    op.drop_table('trading_decision_proposals')
    op.drop_table('trading_decision_sessions')
