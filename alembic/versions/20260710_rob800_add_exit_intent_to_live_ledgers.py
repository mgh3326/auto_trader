"""ROB-800 add exit_intent to live ledgers

Adds the ``exit_intent`` column to ``review.live_order_ledger`` and
``review.kis_live_order_ledger`` so the send-time intent (currently only
``loss_cut``) is recorded alongside the post-reconcile ``exit_reason``.

Additive only: nullable TEXT, default NULL, no NOT NULL, no CHECK, no FK.
Legacy rows are NULL until the next loss-cut order writes a real value.

Revision ID: 20260710_rob800_exit_intent
Revises: 20260707_rob757_toss_fill_poller
Create Date: 2026-07-10 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260710_rob800_exit_intent"
down_revision: str | None = "20260707_rob757_toss_fill_poller"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_order_ledger",
        sa.Column("exit_intent", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "kis_live_order_ledger",
        sa.Column("exit_intent", sa.Text(), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column("live_order_ledger", "exit_intent", schema="review")
    op.drop_column("kis_live_order_ledger", "exit_intent", schema="review")
