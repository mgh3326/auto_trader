"""rob406 merge cancelled with main heads

Revision ID: f09ae24969b0
Revises: 4e7bff9dad80, rob337_rob403_merge_heads
Create Date: 2026-06-02 05:40:23.575785

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f09ae24969b0"
down_revision: str | Sequence[str] | None = (
    "4e7bff9dad80",
    "rob337_rob403_merge_heads",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
