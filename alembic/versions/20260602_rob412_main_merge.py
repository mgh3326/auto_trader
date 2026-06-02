"""ROB-412 repair branch merge into current main head.

Revision ID: 20260602_rob412_main_merge
Revises: 20260602_rob398s3, 20260602_rob412_repair
Create Date: 2026-06-02

Keep the production repair revision identical to the already-deployed production
revision, then merge it with the current main Alembic lineage so `main` remains a
single-head graph.
"""

from collections.abc import Sequence

revision: str = "20260602_rob412_main_merge"
down_revision: str | Sequence[str] | None = (
    "20260602_rob398s3",
    "20260602_rob412_repair",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op merge revision."""
    return None


def downgrade() -> None:
    """No-op merge revision."""
    return None
