"""rob405a merge with main heads

Revision ID: 075d44e505b9
Revises: 2489d4709dec, d8ed14023ef2
Create Date: 2026-06-02 06:23:10.976762

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "075d44e505b9"
down_revision: str | Sequence[str] | None = ("2489d4709dec", "d8ed14023ef2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
