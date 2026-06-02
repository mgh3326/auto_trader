"""merge ROB-422 fundamentals snapshot and ROB-423 news-citation migration heads

Revision ID: rob422_rob423_merge_heads
Revises: rob422_fin_fundamentals, 20260603_rob423_news
Create Date: 2026-06-03 00:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "rob422_rob423_merge_heads"
down_revision: str | Sequence[str] | None = (
    "rob422_fin_fundamentals",
    "20260603_rob423_news",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
