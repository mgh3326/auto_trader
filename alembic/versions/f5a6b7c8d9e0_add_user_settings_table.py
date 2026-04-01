"""add_user_settings_table

Revision ID: f5a6b7c8d9e0
Revises: d6ed88375237
Create Date: 2026-04-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "d6ed88375237"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create user_settings table with JSONB value storage."""
    op.execute("""
        CREATE TABLE user_settings (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(user_id, key)
        )
    """)
    op.execute("CREATE INDEX ix_user_settings_user_id ON user_settings (user_id)")


def downgrade() -> None:
    """Drop user_settings table."""
    op.execute("DROP INDEX IF EXISTS ix_user_settings_user_id")
    op.execute("DROP TABLE IF EXISTS user_settings")
