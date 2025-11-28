"""add_symbol_trade_settings_table

Revision ID: a69eac660fba
Revises: 3c24a5cf6f5e
Create Date: 2025-11-29 08:07:41.798084

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a69eac660fba'
down_revision: Union[str, Sequence[str], None] = '3c24a5cf6f5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Use raw SQL to avoid enum recreation issue
    op.execute("""
        CREATE TABLE symbol_trade_settings (
            id BIGSERIAL,
            symbol TEXT NOT NULL,
            instrument_type instrument_type NOT NULL,
            buy_quantity_per_order NUMERIC(18, 8) NOT NULL,
            exchange_code TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            note TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT pk_symbol_trade_settings PRIMARY KEY (id)
        )
    """)
    op.execute("CREATE UNIQUE INDEX ix_symbol_trade_settings_symbol ON symbol_trade_settings (symbol)")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_symbol_trade_settings_symbol'), table_name='symbol_trade_settings')
    op.drop_table('symbol_trade_settings')
