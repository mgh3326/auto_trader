"""merge hk6302 and current main migration heads

Revision ID: 6f28395ebfc2
Revises: 20261126_hk6750, 20261127_hk6302
Create Date: 2026-11-26 19:19:07.214428

"""

# expand-contract: contract-step 6302

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6f28395ebfc2"
down_revision: str | Sequence[str] | None = (
    "20261126_hk6750",
    "20261127_hk6302",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
