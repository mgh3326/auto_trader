"""merge heads

Revision ID: 9ef8171137c3
Revises: c6358fdef6fd, rob337_add_watch_recommendation
Create Date: 2026-06-01 23:15:26.804664

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9ef8171137c3'
down_revision: Union[str, Sequence[str], None] = ('c6358fdef6fd', 'rob337_add_watch_recommendation')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
