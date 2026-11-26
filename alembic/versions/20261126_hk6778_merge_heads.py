"""Merge the 6751 daily-plan and 6758 movers heads (no schema change).

Revision ID: 20261126_hk6778_merge
Revises: 20261126_hk6751_daily_plan, 20261126_hk6758_kr_mover_val
Create Date: 2026-11-26

#2632 (hk 6751) and #2636 (hk 6758) each added migrations on 20261126_hk6750, so main
had two heads. Both released revisions keep their ancestry; this empty merge revision
joins them so `alembic upgrade head` applies whichever side a database is missing.
"""

from alembic import op  # noqa: F401  (required module shape for the migration lint)

revision = "20261126_hk6778_merge"
down_revision = ("20261126_hk6751_daily_plan", "20261126_hk6758_kr_mover_val")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge point only."""


def downgrade() -> None:
    """Merge point only."""
