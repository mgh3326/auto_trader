"""ROB-714 live ledger correlation_id

Revision ID: 20260705_rob714
Revises: 20260705_rob705
Create Date: 2026-07-05 00:00:00.000000

Additive provenance spine for the LIVE learning loop. Adds a nullable
``correlation_id`` column to the three live order ledgers
(``kis_live_order_ledger``, ``live_order_ledger``, ``toss_live_order_ledger``)
and an index per ledger so reconcile-time journal backfill and downstream
``/insights`` forecast↔retrospective joins can resolve by id.

The id is minted at SEND time by ``app.services.live_correlation`` and is
NULL for legacy rows. No CHECK / NOT NULL / FK — purely additive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260705_rob714"
down_revision: str | Sequence[str] | None = "20260705_rob705"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("kis_live_order_ledger", "live_order_ledger", "toss_live_order_ledger")
_INDEXES = {
    "kis_live_order_ledger": "ix_kis_live_ledger_correlation_id",
    "live_order_ledger": "ix_live_ledger_correlation_id",
    "toss_live_order_ledger": "ix_toss_live_ledger_correlation_id",
}


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("correlation_id", sa.Text(), nullable=True),
            schema="review",
        )
        op.create_index(
            _INDEXES[table], table, ["correlation_id"], schema="review"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(_INDEXES[table], table_name=table, schema="review")
        op.drop_column(table, "correlation_id", schema="review")
