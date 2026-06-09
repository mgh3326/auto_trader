"""rob406 merge heads after 402

Revision ID: 2489d4709dec
Revises: 307953861e78, 998c00e45cf1, f09ae24969b0
Create Date: 2026-06-02 05:48:02.748570

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "2489d4709dec"
down_revision: str | Sequence[str] | None = (
    "307953861e78",
    "998c00e45cf1",
    "f09ae24969b0",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
