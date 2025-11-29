"""add buy_price_levels to symbol_trade_settings

Revision ID: d6ed88375237
Revises: a135dbde152e
Create Date: 2025-11-29 16:03:55.393404

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd6ed88375237'
down_revision: Union[str, Sequence[str], None] = 'a135dbde152e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 기존 데이터가 있으면 기본값 4로 설정
    op.add_column('symbol_trade_settings', sa.Column('buy_price_levels', sa.Integer(), nullable=True))
    op.execute("UPDATE symbol_trade_settings SET buy_price_levels = 4 WHERE buy_price_levels IS NULL")
    op.alter_column('symbol_trade_settings', 'buy_price_levels', nullable=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('symbol_trade_settings', 'buy_price_levels')
