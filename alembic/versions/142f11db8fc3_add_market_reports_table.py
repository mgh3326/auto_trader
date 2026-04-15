"""add market_reports table

Revision ID: 142f11db8fc3
Revises: 2bbc1aab9f3e
Create Date: 2026-04-15 09:10:36.996075

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '142f11db8fc3'
down_revision: Union[str, Sequence[str], None] = '2bbc1aab9f3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('market_reports',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('report_type', sa.String(length=50), nullable=False, comment='리포트 타입 (daily_brief, kr_morning, crypto_scan)'),
    sa.Column('report_date', sa.Date(), nullable=False, comment='리포트 대상 날짜'),
    sa.Column('market', sa.String(length=20), nullable=False, comment='시장 (kr, us, crypto, all)'),
    sa.Column('title', sa.String(length=500), nullable=True, comment='리포트 제목'),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='구조화된 리포트 데이터'),
    sa.Column('summary', sa.Text(), nullable=True, comment='사람이 읽을 수 있는 요약'),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='추가 메타데이터 (소스, 지표 등)'),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, comment='데이터 생성일시'),
    sa.Column('updated_at', sa.DateTime(), nullable=True, comment='데이터 수정일시'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_market_reports_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_market_reports')),
    sa.UniqueConstraint('report_type', 'report_date', 'market', 'user_id', name='uq_market_reports_type_date_market_user'),
    )
    op.create_index('ix_market_reports_market', 'market_reports', ['market'], unique=False)
    op.create_index('ix_market_reports_type_date', 'market_reports', ['report_type', 'report_date'], unique=False)
    op.create_index(op.f('ix_market_reports_user_id'), 'market_reports', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_market_reports_user_id'), table_name='market_reports')
    op.drop_index('ix_market_reports_type_date', table_name='market_reports')
    op.drop_index('ix_market_reports_market', table_name='market_reports')
    op.drop_table('market_reports')
