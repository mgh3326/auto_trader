"""Add observation-only rung void-reason groups (ROB-s257 E-2).

The column is nullable and no historical rows are backfilled.  Existing rows
with a free-text reason are projected as ``unclassified`` by the read surface;
the service records a group only for new reason writes.  Applying this
migration changes no state transition, send, resubmission, or broker behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models.rung_reason_vocabulary import RUNG_VOID_REASON_GROUPS, sql_in_list

revision: str = "20260824_s257_rung_reason"
down_revision: str | Sequence[str] | None = "20260823_screener_pick_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_order_proposal_rungs_order_proposal_rungs_void_reason_group"
_CHECK_SQL = (
    "void_reason_group IS NULL OR void_reason_group IN ("
    + sql_in_list(RUNG_VOID_REASON_GROUPS)
    + ")"
)


def upgrade() -> None:
    """Add the nullable group and its closed-world CHECK only."""
    op.add_column(
        "order_proposal_rungs",
        sa.Column("void_reason_group", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        _CHECK_NAME,
        "order_proposal_rungs",
        _CHECK_SQL,
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        _CHECK_NAME,
        "order_proposal_rungs",
        schema="review",
        type_="check",
    )
    op.drop_column("order_proposal_rungs", "void_reason_group", schema="review")
