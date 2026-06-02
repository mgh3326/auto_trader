"""merge heads rob398s2 + rob405c

Revision ID: bb4fb793a865
Revises: 20260602_rob398s2, 91097f38827e
Create Date: 2026-06-02 08:09:06.071625

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "bb4fb793a865"
down_revision: str | Sequence[str] | None = ("20260602_rob398s2", "91097f38827e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
