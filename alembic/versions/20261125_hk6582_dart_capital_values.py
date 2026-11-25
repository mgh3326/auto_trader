"""Preserve exact DART capital values and missing-input reasons.

Revision ID: 20261125_hk6582_dart_capital
Revises: 20261125_hk6579_benzinga_pro
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261125_hk6582_dart_capital"
down_revision = "20261125_hk6579_benzinga_pro"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration-cycle fixtures create current metadata before stamping an older revision.
    op.add_column(
        "market_event_values",
        sa.Column("value_details", postgresql.JSONB(none_as_null=True), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.execute("LOCK TABLE market_event_values IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM market_event_values WHERE value_details IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while DART capital evidence exists';
            END IF;
        END;
        $$;
    """)
    op.drop_column("market_event_values", "value_details")
