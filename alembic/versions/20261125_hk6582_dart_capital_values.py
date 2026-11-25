"""Preserve exact DART capital values and missing-input reasons.

Revision ID: 20261125_hk6582_dart_capital
Revises: 20261125_hk6513_benzinga
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261125_hk6582_dart_capital"
down_revision = "20261125_hk6513_benzinga"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "market_event_values",
        sa.Column("value_details", postgresql.JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_event_values", "value_details")
