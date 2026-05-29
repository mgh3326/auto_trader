"""rob352 per-item cited_snapshot_uuids

Revision ID: 20260529_rob352
Revises: 20260527_rob329
Create Date: 2026-05-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260529_rob352"
down_revision: str | Sequence[str] | None = "20260527_rob329"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investment_report_items",
        sa.Column(
            "cited_snapshot_uuids",
            sa.ARRAY(sa.UUID()),
            server_default=sa.text("ARRAY[]::uuid[]"),
            nullable=False,
        ),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column(
        "investment_report_items", "cited_snapshot_uuids", schema="review"
    )
