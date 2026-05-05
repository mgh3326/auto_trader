"""add research pipeline tables (ROB-112)

Revision ID: f7891bf9789f
Revises: 9b7c6d5e4f32
Create Date: 2026-05-05 14:16:09.254064

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f7891bf9789f'
down_revision: Union[str, Sequence[str], None] = '9b7c6d5e4f32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. research_sessions
    op.create_table(
        'research_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('stock_info_id', sa.Integer(), nullable=False),
        sa.Column('research_run_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='open', nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open','finalized','failed','cancelled')", name='ck_research_sessions_status'),
        sa.ForeignKeyConstraint(['research_run_id'], ['research_runs.id'], ),
        sa.ForeignKeyConstraint(['stock_info_id'], ['stock_info.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_research_sessions_id'), 'research_sessions', ['id'], unique=False)
    op.create_index(op.f('ix_research_sessions_research_run_id'), 'research_sessions', ['research_run_id'], unique=False)
    op.create_index(op.f('ix_research_sessions_stock_info_id'), 'research_sessions', ['stock_info_id'], unique=False)

    # 2. stage_analysis
    op.create_table(
        'stage_analysis',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('stage_type', sa.String(length=32), nullable=False),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.Integer(), nullable=False),
        sa.Column('signals', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_freshness', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('model_name', sa.String(length=100), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=True),
        sa.Column('snapshot_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('confidence BETWEEN 0 AND 100', name='ck_stage_analysis_confidence_range'),
        sa.CheckConstraint("stage_type IN ('market','news','fundamentals','social')", name='ck_stage_analysis_stage_type'),
        sa.CheckConstraint("verdict IN ('bull','bear','neutral','unavailable')", name='ck_stage_analysis_verdict'),
        sa.ForeignKeyConstraint(['session_id'], ['research_sessions.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_stage_analysis_id'), 'stage_analysis', ['id'], unique=False)
    op.create_index(op.f('ix_stage_analysis_session_id'), 'stage_analysis', ['session_id'], unique=False)
    op.create_index('ix_stage_analysis_session_stage_executed', 'stage_analysis', ['session_id', 'stage_type', 'executed_at'], unique=False)

    # 3. research_summaries
    op.create_table(
        'research_summaries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('decision', sa.String(length=8), nullable=False),
        sa.Column('confidence', sa.Integer(), nullable=False),
        sa.Column('bull_arguments', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('bear_arguments', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('price_analysis', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('detailed_text', sa.Text(), nullable=True),
        sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('model_name', sa.String(length=100), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=True),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('token_input', sa.Integer(), nullable=True),
        sa.Column('token_output', sa.Integer(), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('confidence BETWEEN 0 AND 100', name='ck_research_summaries_confidence_range'),
        sa.CheckConstraint("decision IN ('buy','hold','sell')", name='ck_research_summaries_decision'),
        sa.ForeignKeyConstraint(['session_id'], ['research_sessions.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_research_summaries_id'), 'research_summaries', ['id'], unique=False)
    op.create_index(op.f('ix_research_summaries_session_id'), 'research_summaries', ['session_id'], unique=False)

    # 4. summary_stage_links
    op.create_table(
        'summary_stage_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=False),
        sa.Column('stage_analysis_id', sa.Integer(), nullable=False),
        sa.Column('weight', sa.Float(), server_default='1.0', nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("direction IN ('support','contradict','context')", name='ck_summary_stage_links_direction'),
        sa.CheckConstraint('weight >= 0 AND weight <= 1', name='ck_summary_stage_links_weight_range'),
        sa.ForeignKeyConstraint(['stage_analysis_id'], ['stage_analysis.id'], ),
        sa.ForeignKeyConstraint(['summary_id'], ['research_summaries.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_summary_stage_links_id'), 'summary_stage_links', ['id'], unique=False)
    op.create_index(op.f('ix_summary_stage_links_stage_analysis_id'), 'summary_stage_links', ['stage_analysis_id'], unique=False)
    op.create_index(op.f('ix_summary_stage_links_summary_id'), 'summary_stage_links', ['summary_id'], unique=False)

    # 5. user_research_notes
    op.create_table(
        'user_research_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['session_id'], ['research_sessions.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_research_notes_id'), 'user_research_notes', ['id'], unique=False)
    op.create_index(op.f('ix_user_research_notes_session_id'), 'user_research_notes', ['session_id'], unique=False)
    op.create_index(op.f('ix_user_research_notes_user_id'), 'user_research_notes', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_user_research_notes_user_id'), table_name='user_research_notes')
    op.drop_index(op.f('ix_user_research_notes_session_id'), table_name='user_research_notes')
    op.drop_index(op.f('ix_user_research_notes_id'), table_name='user_research_notes')
    op.drop_table('user_research_notes')

    op.drop_index(op.f('ix_summary_stage_links_summary_id'), table_name='summary_stage_links')
    op.drop_index(op.f('ix_summary_stage_links_stage_analysis_id'), table_name='summary_stage_links')
    op.drop_index(op.f('ix_summary_stage_links_id'), table_name='summary_stage_links')
    op.drop_table('summary_stage_links')

    op.drop_index(op.f('ix_research_summaries_session_id'), table_name='research_summaries')
    op.drop_index(op.f('ix_research_summaries_id'), table_name='research_summaries')
    op.drop_table('research_summaries')

    op.drop_index('ix_stage_analysis_session_stage_executed', table_name='stage_analysis')
    op.drop_index(op.f('ix_stage_analysis_session_id'), table_name='stage_analysis')
    op.drop_index(op.f('ix_stage_analysis_id'), table_name='stage_analysis')
    op.drop_table('stage_analysis')

    op.drop_index(op.f('ix_research_sessions_stock_info_id'), table_name='research_sessions')
    op.drop_index(op.f('ix_research_sessions_research_run_id'), table_name='research_sessions')
    op.drop_index(op.f('ix_research_sessions_id'), table_name='research_sessions')
    op.drop_table('research_sessions')
