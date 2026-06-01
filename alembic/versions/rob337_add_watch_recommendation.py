"""ROB-337 add investment_report_items.watch_recommendation

Revision ID: rob337_add_watch_recommendation
Revises: 14fa36b85d0a
Create Date: 2026-06-01

Additive nullable JSONB column for advisory buy-review price thresholds.
Existing rows keep NULL. No CHECK. Production apply is operator-gated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "rob337_add_watch_recommendation"
down_revision: Union[str, Sequence[str], None] = "14fa36b85d0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "investment_report_items",
        sa.Column("watch_recommendation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column("investment_report_items", "watch_recommendation", schema="review")
