"""merge toss warnings and ledger heads

Revision ID: ec2fbbc5898c
Revises: 20260612_rob534_rob538_merge, d82e093e7590
Create Date: 2026-06-12 18:33:38.505867

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ec2fbbc5898c'
down_revision: Union[str, Sequence[str], None] = ('20260612_rob534_rob538_merge', 'd82e093e7590')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
