"""rob473 rob474 merge heads

Revision ID: c07e44daf745
Revises: 20260609_rob473, 20260609_rob474
Create Date: 2026-06-09 18:34:29.509214

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = 'c07e44daf745'
down_revision: str | Sequence[str] | None = ('20260609_rob473', '20260609_rob474')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
