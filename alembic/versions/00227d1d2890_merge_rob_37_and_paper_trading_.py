"""merge ROB-37 and paper-trading migration heads

Revision ID: 00227d1d2890
Revises: 0f2f4afaf556, f29d2ab2ca96
Create Date: 2026-04-15 12:14:04.303310

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "00227d1d2890"
down_revision: str | Sequence[str] | None = ("0f2f4afaf556", "f29d2ab2ca96")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
