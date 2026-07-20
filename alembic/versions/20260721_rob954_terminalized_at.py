"""rob954_alpaca_terminalized_at

Revision ID: 20260721_rob954_terminalized_at
Revises: 20260717_rob920_alpaca_canceled
Create Date: 2026-07-21 00:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260721_rob954_terminalized_at"
down_revision: str | Sequence[str] | None = "20260717_rob920_alpaca_canceled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "alpaca_paper_order_ledger"
_SCHEMA = "review"
_INDEX = "ix_alpaca_paper_ledger_terminalized_at"


def upgrade() -> None:
    """Add the immutable first-terminal-transition timestamp and scan index."""
    op.add_column(
        _TABLE,
        sa.Column("terminalized_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["terminalized_at"],
        unique=False,
        schema=_SCHEMA,
    )

    # Intentionally no data backfill: updated_at is mutable metadata time, so
    # copying it would fabricate a terminal transition and preserve the window
    # churn bug in historical data. The scanner explicitly falls back to stable
    # created_at for these pre-migration NULL terminal rows.


def downgrade() -> None:
    """Remove the terminal transition timestamp and its index."""
    op.drop_index(_INDEX, table_name=_TABLE, schema=_SCHEMA)
    op.drop_column(_TABLE, "terminalized_at", schema=_SCHEMA)
