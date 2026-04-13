"""merge paper trading migrations

Revision ID: 2bbc1aab9f3e
Revises: 1666768ca8ff, db555723284f
Create Date: 2026-04-14 07:25:25.305767

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bbc1aab9f3e'
down_revision: Union[str, Sequence[str], None] = ('1666768ca8ff', 'db555723284f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
