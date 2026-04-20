"""merge duplicate heads

Revision ID: f29d2ab2ca96
Revises: 142f11db8fc3, 2bbc1aab9f3e
Create Date: 2026-04-15 11:33:01.917380

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f29d2ab2ca96'
down_revision: Union[str, Sequence[str], None] = ('142f11db8fc3', '2bbc1aab9f3e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
